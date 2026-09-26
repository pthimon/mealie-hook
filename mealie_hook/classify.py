"""Category, tags and tools: the model picks from the live vocabulary, code enforces rules.

The schema is built from Mealie's current organizers, so the model can only choose names
that already exist -- a near-miss would otherwise create a duplicate organizer. Nothing
here ever creates a category, tag or tool.
"""

import json
import re
from dataclasses import dataclass, field

from .foods import loose, plural_candidates
from .llm import LLM
from .models import classification_reply
from .rules import Vocabulary

# The appliance, not the technique: "slow-cooked" chicken done in a casserole in the oven
# is not a Slow Cooker recipe.
_SLOW_COOK = re.compile(r"slow[- ]?cooker", re.I)


def recipe_brief(recipe: dict, equipment: list[str]) -> dict:
    """The facts the classifier sees. Kept small: name, source, foods, a taste of method."""
    foods = []
    for ing in recipe.get("recipeIngredient") or []:
        f = ing.get("food")
        foods.append(f["name"] if isinstance(f, dict) and f.get("name")
                     else (ing.get("originalText") or ing.get("note") or ""))
    steps = [s.get("text") or "" for s in recipe.get("recipeInstructions") or []]
    return {
        "name": recipe.get("name"),
        "description": recipe.get("description") or "",
        "source": recipe.get("orgURL") or "",
        "servings": recipe.get("recipeServings"),
        "yield": recipe.get("recipeYield") or "",
        "ingredients": [f for f in foods if f],
        "equipment_lines": equipment,
        "method": " ".join(steps)[:1500],
    }


@dataclass
class Classification:
    category: str | None
    tags: list[str]
    tools: list[str]
    dish: str = ""
    new_protein: str | None = None
    flags: list[str] = field(default_factory=list)


def call_model(llm: LLM, system: str, brief: dict, categories: list[str],
               tags: list[str], tools: list[str]) -> Classification:
    reply = llm.structured(
        [{"role": "system", "content": system},
         {"role": "user", "content": json.dumps(brief, ensure_ascii=False)}],
        classification_reply(categories, tags, tools), name="classify")
    return Classification(reply.category, list(reply.tags), list(reply.tools), reply.dish,
                          reply.new_protein)


def match_tag(name: str, known: set[str]) -> str | None:
    """Existing tag with the same name, ignoring case, punctuation and plural."""
    by_key = {loose(t): t for t in known}
    for cand in {loose(name)} | {loose(c) for c in plural_candidates(name)}:
        if cand in by_key:
            return by_key[cand]
    for t in known:                          # the proposal is a plural of an existing tag
        if loose(name) in {loose(c) for c in plural_candidates(t)}:
            return t
    return None


def apply_rules(c: Classification, recipe: dict, known_tags: set[str],
                known_tools: set[str], banned_tags: set[str], vocab: Vocabulary) -> Classification:
    """House rules that must hold whatever the model said. Returns a new Classification.

    Every rule keys on a ROLE from the vocabulary, never on a name, so renaming a tag in
    Mealie and in the vocabulary keeps the rule working; drift() reports a rule whose role
    has no live name.
    """
    tags = [t for t in dict.fromkeys(c.tags) if t in known_tags and t not in banned_tags]
    tools = [t for t in dict.fromkeys(c.tools) if t in known_tools]
    flags = list(c.flags)
    mains = set(vocab.with_role("categories", "main"))
    summer = [t for t in vocab.with_role("tags", "summer") if t in known_tags]
    winter = [t for t in vocab.with_role("tags", "winter") if t in known_tags]
    slow = [t for t in vocab.with_role("tools", "slow-cooker") if t in known_tools]

    proposal = (c.new_protein or "").strip()
    if proposal and c.category in mains:
        existing = match_tag(proposal, known_tags - banned_tags)
        if existing:
            if existing not in tags:
                tags.append(existing)
        else:
            flags.append(f"headline protein {proposal!r} has no tag; create a "
                         f"{proposal.capitalize()!r} tag in Mealie and add it if you want one")

    haystack = " ".join(
        [recipe.get("name") or ""]
        + [(i.get("originalText") or i.get("note") or "") for i in recipe.get("recipeIngredient") or []]
        + [(s.get("text") or "") for s in recipe.get("recipeInstructions") or []])
    if slow and _SLOW_COOK.search(haystack) and not set(slow) & set(tools):
        tools.append(slow[0])

    # Anything slow-cooked is a winter dish here.
    if set(slow) & set(tools) and winter:
        tags = [t for t in tags if t not in summer]
        if not set(winter) & set(tags):
            tags.append(winter[0])

    if c.category in mains:
        not_mains = set(vocab.with_role("tags", "not-on-mains"))
        tags = [t for t in tags if t not in not_mains]

    # No recipe is both seasons; if the model says so it has no view, so neither.
    if set(summer) & set(tags) and set(winter) & set(tags):
        tags = [t for t in tags if t not in set(summer) | set(winter)]
        flags.append("classifier tagged both summer and winter; dropped both")

    return Classification(c.category, tags, tools, c.dish, c.new_protein, flags)

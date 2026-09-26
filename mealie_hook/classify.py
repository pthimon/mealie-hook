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

# The appliance, not the technique: "slow-cooked" chicken done in a casserole in the oven
# is not a Slow Cooker recipe.
_SLOW_COOK = re.compile(r"slow[- ]?cooker", re.I)


# Character tags describe sweet, breakfast and snack recipes. Across the 269 hand-filed
# recipes these are (all but once) never on a Dinner or Lunch; Curry, Nuts & Seeds,
# Fermented and Frozen do appear on mains, so they are not listed.
NOT_ON_MAINS = {"Savoury", "Dairy", "Fruit", "Oats", "Chocolate", "Citrus"}
MAINS = {"Dinner", "Lunch"}


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
                known_tools: set[str], banned_tags: set[str]) -> Classification:
    """House rules that must hold whatever the model said. Returns a new Classification."""
    tags = [t for t in dict.fromkeys(c.tags) if t in known_tags and t not in banned_tags]
    tools = [t for t in dict.fromkeys(c.tools) if t in known_tools]
    flags = list(c.flags)

    proposal = (c.new_protein or "").strip()
    if proposal and c.category in MAINS:
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
    if _SLOW_COOK.search(haystack) and "Slow Cooker" in known_tools and "Slow Cooker" not in tools:
        tools.append("Slow Cooker")

    # Anything slow-cooked is a winter dish here.
    if "Slow Cooker" in tools and "Winter" in known_tags:
        tags = [t for t in tags if t != "Summer"]
        if "Winter" not in tags:
            tags.append("Winter")

    if c.category in MAINS:
        tags = [t for t in tags if t not in NOT_ON_MAINS]

    # No recipe is both seasons; if the model says so it has no view, so neither.
    if "Summer" in tags and "Winter" in tags:
        tags = [t for t in tags if t not in ("Summer", "Winter")]
        flags.append("classifier tagged both Summer and Winter; dropped both")

    return Classification(c.category, tags, tools, c.dish, c.new_protein, flags)

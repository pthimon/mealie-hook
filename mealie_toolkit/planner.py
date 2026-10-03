"""Plan -> list: the week's meal plan, scaled per recipe, onto a Mealie shopping list.

Mealie's own "add planner to list" dialog adds every recipe at its stated size (or a
multiple, by planning it twice). This does the same job with a size per recipe: recipes
that state their servings default to PLAN_DEFAULT_SERVINGS; ones that only state a yield
("25 items", "6 cakes") or nothing default to the whole recipe, because "2 servings of
cookies" means nothing. The size last used for a recipe is remembered and becomes its
default next time, so a "whole batch" choice for a bake sticks.

The adding itself is Mealie's own endpoint, given each recipe's scale and the ticked
ingredients unscaled: Mealie multiplies, merges into existing items and keeps the recipe
references, exactly as its dialog does. Ingredients travel as `referenceId`s and are
re-read from the recipe at add time, so the page never round-trips whole ingredient
objects.

Sub-recipes (an ingredient that is another recipe, common in the ZOE book) are shown as
their own blocks, as in Mealie's dialog, at the parent's scale times the ingredient's
quantity, and are added as recipes of their own.

Plan entries already added from this page are remembered (by plan entry id), shown as
added and left unticked, so a mid-week top-up shop does not add Monday's dinner again.
"""

from datetime import date, timedelta

from . import bought
from .mealie import Mealie
from .state import State

ENTRY_ORDER = ["breakfast", "lunch", "dinner", "side", "snack", "drink", "dessert"]
MAX_DEPTH = 3
MAX_SCALE = 50


def base_of(recipe: dict) -> dict:
    """What the size control counts: servings, the stated yield, or whole batches."""
    servings = recipe.get("recipeServings") or 0
    if servings > 0:
        return {"kind": "servings", "amount": servings, "unit": "servings"}
    yq = recipe.get("recipeYieldQuantity") or 0
    if yq > 0:
        return {"kind": "yield", "amount": yq,
                "unit": (recipe.get("recipeYield") or "").strip() or "items"}
    return {"kind": "batch", "amount": 1, "unit": "batch"}


def _row(ing: dict, household: str, record: dict[str, str]) -> dict:
    food, unit = ing.get("food") or {}, ing.get("unit") or {}
    on_hand = household in (food.get("householdsWithIngredientFood") or [])
    return {
        "ref": ing.get("referenceId"),
        "qty": ing.get("quantity") or 0,
        "unit": unit.get("name") or "", "unit_plural": unit.get("pluralName") or "",
        "food": food.get("name") or "", "food_plural": food.get("pluralName") or "",
        "note": (ing.get("note") or "").strip(),
        "label": (food.get("label") or {}).get("name") or "",
        "text": (ing.get("note") or ing.get("originalText") or ing.get("display") or "").strip(),
        "on_hand": on_hand, "checked": not on_hand,
        "bought": bought.last_bought(record, food.get("name") or "", food.get("pluralName") or "")
                  if food.get("name") else None,
    }


class _Recipes:
    """Full recipes, fetched once per request."""

    def __init__(self, mealie: Mealie):
        self.mealie, self.cache = mealie, {}

    def get(self, slug: str) -> dict:
        if slug not in self.cache:
            self.cache[slug] = self.mealie.recipe(slug)
        return self.cache[slug]


def _block(recipes: _Recipes, slug: str, household: str, url_base: str,
           record: dict[str, str], seen: tuple = ()) -> dict:
    r = recipes.get(slug)
    sections, subs = [], []
    for ing in r.get("recipeIngredient") or []:
        sub = ing.get("referencedRecipe")
        if sub:
            if sub.get("slug") and sub["slug"] not in seen and len(seen) < MAX_DEPTH:
                subs.append({"factor": ing.get("quantity") or 1,
                             **_block(recipes, sub["slug"], household, url_base, record,
                                      seen + (slug,))})
            continue
        if ing.get("title") or not sections:
            sections.append({"title": ing.get("title") or "", "rows": []})
        sections[-1]["rows"].append(_row(ing, household, record))
    return {"id": r["id"], "slug": r["slug"], "name": r["name"], "url": f"{url_base}{r['slug']}",
            "base": base_of(r), "sections": [s for s in sections if s["rows"]], "subs": subs}


def default_amount(block: dict, remembered: float | None, default_servings: float) -> float:
    base = block["base"]
    if remembered:
        return round(remembered * base["amount"], 2)
    if base["kind"] == "servings":
        return default_servings
    return base["amount"]


def build(mealie: Mealie, state: State, start: str, end: str,
          default_servings: float, lists: list[dict] | None = None,
          record: dict[str, str] | None = None) -> dict:
    """`record` is the last-bought record if the caller has just harvested it."""
    if record is None:
        record = bought.harvest(mealie, state, lists)
    household = mealie.household_self().get("slug") or ""
    group = mealie.group_self().get("slug") or ""
    url_base = f"/g/{group}/r/" if group else "/r/"
    added = state.data.get("plan_added") or {}
    remembered = state.data.get("plan_scales") or {}
    recipes = _Recipes(mealie)

    def order(e):
        t = e.get("entryType") or ""
        return (e.get("date") or "", ENTRY_ORDER.index(t) if t in ENTRY_ORDER else 99)

    entries = []
    for e in sorted(mealie.mealplans(start, end), key=order):
        out = {"id": e["id"], "date": e["date"], "type": e.get("entryType") or "",
               "title": e.get("title") or "", "text": e.get("text") or "",
               "added": (added.get(str(e["id"])) or {}).get("at")}
        slug = (e.get("recipe") or {}).get("slug")
        if slug:
            block = _block(recipes, slug, household, url_base, record)
            out["recipe"] = block
            out["remembered"] = remembered.get(slug)
            out["amount"] = default_amount(block, remembered.get(slug), default_servings)
        entries.append(out)
    return {"start": start, "end": end, "entries": entries,
            "default_servings": default_servings}


def add(mealie: Mealie, state: State, list_id: str, items: list[dict],
        entries: list[dict]) -> dict:
    """items: [{slug, scale, refs}] -- one per recipe block, sub-recipes included.
    entries: [{id, date, slug, scale}] -- the plan entries this covers, to remember."""
    recipes = _Recipes(mealie)
    body, n_ings, missing = [], 0, 0
    for it in items:
        scale = float(it["scale"])
        if not 0 < scale <= MAX_SCALE:
            raise ValueError(f"scale {scale:g} for {it['slug']} is out of range")
        refs = set(it.get("refs") or [])
        if not refs:
            continue
        r = recipes.get(it["slug"])
        ings = r.get("recipeIngredient") or []
        picked = [i for i in ings
                  if i.get("referenceId") in refs and not i.get("referencedRecipe")]
        # refs no longer in the recipe: it was edited since the page loaded
        missing += len(refs - {i.get("referenceId") for i in ings})
        if picked:
            body.append({"recipeId": r["id"], "recipeIncrementQuantity": scale,
                         "recipeIngredients": picked})
            n_ings += len(picked)
    if body:
        mealie.add_recipes_to_list(list_id, body)
    state.plan_added(entries)
    return {"recipes": len(body), "ingredients": n_ings, "missing": missing}


def default_range(today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    return today.isoformat(), (today + timedelta(days=6)).isoformat()

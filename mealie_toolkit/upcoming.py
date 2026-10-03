"""Upcoming: what the meals still to cook need, ingredient by ingredient.

The meal plan from today on, each meal at the size it was added to the shopping list with
(Mealie's plan has no sizes, so Plan -> list records them), adds up to how much of each
food is still spoken for. That answers "is the leek needed for anything?" -- if not, it
can go in whatever is being cooked now.

A meal's size comes from, in order: the size it was added to the list with; for meals added
before sizes were recorded per meal, the recipe's last-used size; otherwise the same
default Plan -> list would start at. What is left is decided by date alone: meals before
today drop off, so the meal planner should hold the day each meal is actually cooked. The
page moves meals between days itself (`move`, `swap`), writing to Mealie's planner, which
is easier on a phone than dragging in Mealie.

Meals planned between the last shop and yesterday are listed too, as "passed", in case the
plan needs straightening out after the fact (the fish was cooked on Monday, not the
planned curry). They count for nothing. "The last shop" is the latest tick-off seen, in
Mealie or from the Shopping tab; a different start can be chosen. Amounts are what the recipes call
for, whatever was bought: spoonfuls included, since the question is what cooking will use,
not what to buy.
"""

from datetime import date, timedelta

from . import bought, planner
from .foods import canonical_unit, loose
from .mealie import Mealie, MealieError
from .state import State, parse_ts

DAYS = 28
PASSED_DAYS = 7          # how far back "passed" looks when no shop has been seen yet
# What Mealie's update takes back; everything but the date is returned as it was.
ENTRY_FIELDS = ("date", "entryType", "title", "text", "recipeId", "id", "groupId", "userId")


def _scale(entry: dict, added: dict, scales: dict) -> tuple[float, str]:
    base = entry["recipe"]["base"]["amount"]
    rec = added.get(str(entry["id"]))
    slug = entry["recipe"]["slug"]
    if rec and rec.get("scale"):
        return rec["scale"], "added"
    if rec and scales.get(slug):
        return scales[slug], "added"
    return entry["amount"] / base, "last used" if entry.get("remembered") else "default"


def last_shop(state: State) -> date | None:
    """The day of the latest tick-off seen: Mealie's ticked items or the Shopping tab."""
    times = [parse_ts(t) for t in [state.data.get("last_tick"),
                                   *(state.data.get("bought") or {}).values()] if t]
    return max(times).date() if times else None


def build(mealie: Mealie, state: State, today: date, default_servings: float,
          days: int = DAYS, since: date | None = None) -> dict:
    record = bought.harvest(mealie, state)         # first, so a shop ticked off just now counts
    shop = last_shop(state)
    chosen = since is not None
    since = since or shop or today - timedelta(days=PASSED_DAYS)
    since = min(since, today)
    view = planner.build(mealie, state, since.isoformat(),
                         (today + timedelta(days=days)).isoformat(), default_servings,
                         record=record)
    added = state.data.get("plan_added") or {}
    scales = state.data.get("plan_scales") or {}
    meals, foods = [], {}

    def walk(block: dict, scale: float, meal: dict, via: str = ""):
        for sec in block["sections"]:
            for r in sec["rows"]:
                key = "f:" + loose(r["food"]) if r["food"] else "t:" + loose(r["text"])
                g = foods.setdefault(key, {
                    "key": key, "food": r["food"], "plural": r["food_plural"],
                    "text": r["text"], "label": r["label"], "amounts": {}, "unit_plurals": {},
                    "first": meal["date"], "uses": []})
                q = (r["qty"] or 0) * scale
                unit = canonical_unit(r["unit"]) or ""
                if q:
                    g["amounts"][unit] = g["amounts"].get(unit, 0) + q
                    if r["unit_plural"]:
                        g["unit_plurals"][unit] = r["unit_plural"]
                g["first"] = min(g["first"], meal["date"])
                g["uses"].append({"meal": meal["id"], "date": meal["date"], "type": meal["type"],
                                  "name": meal["name"] + (f" ({via})" if via else ""),
                                  "qty": q, "unit": r["unit"], "unit_plural": r["unit_plural"],
                                  "note": r["note"], "text": r["text"]})
        for sub in block["subs"]:
            walk(sub, scale * sub["factor"], meal, sub["name"])

    for e in view["entries"]:
        if not e.get("recipe"):
            continue
        scale, source = _scale(e, added, scales)
        b = e["recipe"]
        meal = {"id": e["id"], "date": e["date"], "type": e["type"], "name": b["name"],
                "url": b["url"], "scale": round(scale, 4), "source": source,
                "servings": round(scale * b["base"]["amount"], 2) if b["base"]["kind"] != "batch"
                else None, "unit": b["base"]["unit"], "passed": e["date"] < today.isoformat()}
        meals.append(meal)
        if not meal["passed"]:
            walk(b, scale, meal)
    order = sorted(foods.values(), key=lambda g: (g["label"] or "~", (g["food"] or g["text"]).lower()))
    return {"today": today.isoformat(), "days": days, "meals": meals, "foods": order,
            "since": since.isoformat(), "since_chosen": chosen,
            "last_shop": shop.isoformat() if shop else None}


def move(mealie: Mealie, entry_id: int, new_date: date) -> dict:
    """Put a planned meal on another day, in Mealie's own planner."""
    e = mealie.mealplan(entry_id)
    old = e["date"]
    if old != new_date.isoformat():
        mealie.update_mealplan(entry_id, {**{k: e.get(k) for k in ENTRY_FIELDS},
                                          "date": new_date.isoformat()})
    return {"id": entry_id, "from": old, "to": new_date.isoformat()}


def swap(mealie: Mealie, a: int, b: int) -> dict:
    """Swap two planned meals' days. If the second write fails the first is put back, so the
    plan never ends up with both meals on one day."""
    ea, eb = mealie.mealplan(a), mealie.mealplan(b)
    if ea["date"] == eb["date"]:
        return {"a": a, "b": b, "dates": [ea["date"], eb["date"]], "changed": False}
    mealie.update_mealplan(a, {**{k: ea.get(k) for k in ENTRY_FIELDS}, "date": eb["date"]})
    try:
        mealie.update_mealplan(b, {**{k: eb.get(k) for k in ENTRY_FIELDS}, "date": ea["date"]})
    except MealieError:
        mealie.update_mealplan(a, {k: ea.get(k) for k in ENTRY_FIELDS})
        raise
    return {"a": a, "b": b, "dates": [eb["date"], ea["date"]], "changed": True}

"""When each food was last bought, as a hint on the combined list.

"Bought" means ticked off a shopping list. Mealie keeps ticked items, and ticking one sets
its `updatedAt`, so the latest ticked item per food is its last purchase. Mealie's history
goes if its checked items are ever deleted, so every page load copies what it finds into
state.json (`bought`: loose food name -> ISO time, latest wins); once seen, a purchase is
kept.

Home Assistant items carry no food and no dates, so they count only when ticked from the
Shopping tab, keyed by their name. That matches a food by its name or plural ("Carrots"
finds carrot via its plural), nothing cleverer.
"""

from .foods import loose
from .mealie import Mealie
from .state import State, now_iso


def harvest(mealie: Mealie, state: State, lists: list[dict] | None = None,
            items_by_list: dict[str, list[dict]] | None = None) -> dict[str, str]:
    """Fold every list's ticked items into the record. `items_by_list` reuses items the
    caller already fetched. Returns the whole record."""
    found: dict[str, str] = {}
    items_by_list = dict(items_by_list or {})
    for lst in lists if lists is not None else mealie.shopping_lists():
        items = items_by_list.get(lst["id"])
        if items is None:
            items = mealie.shopping_list(lst["id"]).get("listItems") or []
        for i in items:
            name = (i.get("food") or {}).get("name")
            ts = i.get("updatedAt")
            if i.get("checked") and name and ts:
                found[loose(name)] = max(found.get(loose(name), ""), ts)
    return state.record_bought(found)


def record_ticked(state: State, mealie_items: list[dict], ha_names: list[str]):
    """Ticked from the Shopping tab just now."""
    at = now_iso()
    names = [(i.get("food") or {}).get("name") for i in mealie_items] + list(ha_names)
    state.record_bought({loose(n): at for n in names if n})


def last_bought(record: dict[str, str], name: str, plural: str = "") -> str | None:
    hits = [record.get(loose(n)) for n in (name, plural) if n]
    return max((h for h in hits if h), default=None)

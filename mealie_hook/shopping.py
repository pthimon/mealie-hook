"""The shopping export: Mealie's list plus Home Assistant's, as lines for a supermarket search.

One line per item, just the shoppable food name by default -- extra words hurt the
supermarket's search matching, and "finely chopped" is a cooking instruction, not something
to buy. Home Assistant holds the free-text things added as they run out; an HA item is hidden
from the export when a Mealie item already covers that food (it is still ticked off with the
rest).

Ticking takes the exact items the page showed, so anything added after it was loaded is
left outstanding.

`since` is for a shop that was never ticked off: Mealie items last added to before it are
split out as "probably bought already", left out of the export, and can be ticked off on
their own. "Last added to" is `updatedAt`, because Mealie merges a recipe's carrots into an
existing carrots item and bumps it, so topped-up items correctly count as new. Home
Assistant items carry no dates at all, so they are never split.
"""

import logging

import httpx

from . import bought
from .foods import norm
from .mealie import Mealie
from .state import State, parse_ts

log = logging.getLogger(__name__)


class HomeAssistant:
    def __init__(self, url: str, token: str, timeout: float = 20):
        self.enabled = bool(url and token)
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=timeout,
                                 headers={"Authorization": f"Bearer {token}"})

    def items(self) -> list[dict]:
        """[{"id", "name", "complete"}] -- HA items carry no timestamps at all."""
        r = self.http.get("/api/shopping_list")
        r.raise_for_status()
        return r.json()

    def tick(self, item_id: str):
        # No bulk endpoint; a partial body is merged, so the name is kept.
        r = self.http.post(f"/api/shopping_list/item/{item_id}", json={"complete": True})
        r.raise_for_status()


def _fmt_qty(q) -> str:
    return f"{q:g}" if q else ""


def mealie_line(item: dict, quantities: bool) -> str:
    food = item.get("food") or {}
    qty = item.get("quantity") or 0
    name = food.get("name")
    if name and qty > 1 and food.get("pluralName"):
        name = food["pluralName"]
    if not name:
        return (item.get("note") or "").strip()   # free-text item: the note is its name
    if not quantities:
        return name
    unit = item.get("unit") or {}
    uname = unit.get("name")
    if uname and qty > 1 and unit.get("pluralName"):
        uname = unit["pluralName"]
    return " ".join([name] + ([_fmt_qty(qty) + (f" {uname}" if uname else "")] if qty else []))


def build(mealie: Mealie, ha: HomeAssistant | None, list_id: str | None = None,
          quantities: bool = False, since: str | None = None) -> dict:
    lists = mealie.shopping_lists()
    if not lists:
        return {"lists": [], "error": "no shopping lists in Mealie"}
    target = next((x for x in lists if x["id"] == list_id), lists[0])
    items = [i for i in (mealie.shopping_list(target["id"]).get("listItems") or [])
             if not i.get("checked")]
    cutoff = parse_ts(since) if since else None
    m_rows, covered = [], set()
    for i in items:
        line = mealie_line(i, quantities)
        added = parse_ts(i.get("updatedAt") or i.get("createdAt"))
        old = bool(cutoff and added and added < cutoff)
        if i.get("food") and not old:
            # only what is still to buy hides an HA duplicate: carrots bought at the forgotten
            # shop must not swallow a newly added HA "carrot"
            covered.add(norm(i["food"].get("name")))
        if line:
            m_rows.append({"id": i["id"], "line": line,
                           "added": added.isoformat() if added else None, "old": old,
                           "detail": (i.get("display") or line).strip(),
                           "label": ((i.get("label") or {}).get("name")
                                     or ((i.get("food") or {}).get("label") or {}).get("name")
                                     or "")})
    h_rows, ha_error = [], None
    if ha and ha.enabled:
        try:
            seen = set(covered)
            for it in ha.items():
                name = (it.get("name") or "").strip()
                if it.get("complete") or not name or not it.get("id"):
                    continue
                dup = norm(name) in seen
                seen.add(norm(name))
                h_rows.append({"id": it["id"], "line": name, "duplicate": dup})
        except Exception as e:
            ha_error = f"could not reach Home Assistant: {type(e).__name__}"
            log.warning("home assistant: %s", e)
    text = "\n".join([r["line"] for r in m_rows if not r["old"]]
                     + [r["line"] for r in h_rows if not r["duplicate"]])
    return {
        "lists": [{"id": x["id"], "name": x["name"]} for x in lists],
        "list": {"id": target["id"], "name": target["name"]},
        "mealie": sorted(m_rows, key=lambda r: (r["label"] or "~", r["line"].lower())),
        "ha": h_rows, "ha_enabled": bool(ha and ha.enabled), "ha_error": ha_error,
        "text": text, "since": cutoff.isoformat() if cutoff else None,
    }


def tick(mealie: Mealie, ha: HomeAssistant | None, list_id: str, mealie_ids: list[str],
         ha_ids: list[str], state: State | None = None) -> dict:
    """Tick off exactly these items. A dead Home Assistant never costs the Mealie half.
    With `state`, what was ticked is recorded as bought now."""
    wanted = set(mealie_ids)
    items = [i for i in (mealie.shopping_list(list_id).get("listItems") or [])
             if i["id"] in wanted and not i.get("checked")]
    done_m = 0
    if items:
        res = mealie.tick_shopping_items(items) or {}
        done_m = len(res.get("updatedItems") or [])
    done_h, failed, ha_names = 0, [], []
    if ha and ha.enabled and ha_ids:
        try:
            names = {it.get("id"): it.get("name") for it in ha.items()}
        except Exception:
            names = {}                      # only the bought record misses out
        for hid in ha_ids:
            try:
                ha.tick(hid)
                done_h += 1
                if names.get(hid):
                    ha_names.append(names[hid])
            except Exception as e:
                failed.append(f"{hid}: {type(e).__name__}")
    if state is not None:
        bought.record_ticked(state, items if done_m else [], ha_names)
    return {"mealie": done_m, "mealie_asked": len(items), "ha": done_h, "ha_failed": failed}

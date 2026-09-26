"""Write the tracking JSON files from Mealie's current state.

Mealie is the source of truth; these files are a readable, diffable record of it, in the
same shapes the old skill's replay commands took.
"""

import json
from pathlib import Path

from .mealie import Mealie


def _dump(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def export(mealie: Mealie, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    recipes = sorted(mealie.recipes(), key=lambda r: r["slug"])
    written = []
    for fname, field, names in (
        ("mealie-categories.json", "recipeCategory", [c["name"] for c in mealie.categories()]),
        ("mealie-tags.json", "tags", [t["name"] for t in mealie.tags()]),
        ("mealie-tools.json", "tools", [t["name"] for t in mealie.tools()]),
    ):
        plan = {n: [] for n in sorted(names, key=str.lower)}
        for r in recipes:
            for o in r.get(field) or []:
                plan.setdefault(o["name"], []).append(r["slug"])
        _dump(out_dir / fname, plan)
        written.append(out_dir / fname)

    labels = mealie.labels()
    order = [x["name"] for x in sorted(labels, key=lambda x: x["name"].lower())]
    lists = mealie.shopping_lists()
    if lists:
        settings = mealie.shopping_list(lists[0]["id"]).get("labelSettings") or []
        ranked = [s["label"]["name"] for s in sorted(settings, key=lambda s: s.get("position", 0))
                  if s.get("label")]
        order = ranked + [n for n in order if n not in ranked]
    by_label: dict[str, list[str]] = {n: [] for n in order}
    for f in sorted(mealie.foods(), key=lambda f: f["name"].lower()):
        name = (f.get("label") or {}).get("name")
        if name:
            by_label.setdefault(name, []).append(f["name"])
    _dump(out_dir / "mealie-food-labels.json",
          {"_colors": {x["name"]: x.get("color") for x in labels}, "_order": order, **by_label})
    written.append(out_dir / "mealie-food-labels.json")
    return written

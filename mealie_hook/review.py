"""The Needs review queue: recipes this service flagged, with the reasons it gave."""

from .foods import norm
from .mealie import Mealie
from .pipeline import REVIEW_NOTE_TITLE


def _tag(mealie: Mealie, name: str) -> dict | None:
    return next((t for t in mealie.tags() if norm(t["name"]) == norm(name)), None)


def flagged(mealie: Mealie, review_tag: str) -> list[dict]:
    tag = _tag(mealie, review_tag)
    if not tag:
        return []
    group = mealie.group_self().get("slug") or ""
    out = []
    for s in mealie.recipes_tagged(tag["id"]):
        r = mealie.recipe(s["slug"])
        note = next((n for n in r.get("notes") or [] if n.get("title") == REVIEW_NOTE_TITLE), None)
        reasons = [ln.lstrip("- ").strip() for ln in (note or {}).get("text", "").splitlines()
                   if ln.strip()]
        out.append({"slug": r["slug"], "name": r["name"], "reasons": reasons,
                    # same origin as Mealie, so a path is enough
                    "url": f"/g/{group}/r/{r['slug']}" if group else f"/r/{r['slug']}",
                    "created": r.get("createdAt")})
    return sorted(out, key=lambda x: x.get("created") or "", reverse=True)


def clear(mealie: Mealie, slug: str, review_tag: str) -> None:
    """Remove the tag and this service's note; nothing else on the recipe changes."""
    r = mealie.recipe(slug)
    r["tags"] = [t for t in r.get("tags") or [] if norm(t["name"]) != norm(review_tag)]
    r["notes"] = [n for n in r.get("notes") or [] if n.get("title") != REVIEW_NOTE_TITLE]
    mealie.put_recipe(slug, r)

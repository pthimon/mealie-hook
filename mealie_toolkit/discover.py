"""Discover: find well-rated BBC Good Food recipes and bulk-import them into Mealie.

Good Food's own search API does the finding: meal type, diet, minimum star rating, time,
cuisine and difficulty are its filters, and `sort=rating` ("Most popular") puts the
recipes with the most ratings first. Premium recipes are paywalled, so they are dropped.

Search results carry no nutrition, so each candidate's page is read for its schema.org
Recipe data -- saturated fat, calories, servings, time -- which is what the page filters on
(Good Food publishes no cholesterol figures). Pages are cached in DATA_DIR, so paging back
and forth or re-running a search costs nothing, and at most a few are fetched at once.

Importing is Mealie's own bulk URL import. That fires the same recipe_created event as an
import from Mealie's UI, so the usual processing (ingredients, nutrition, category, tags,
aisles) follows on its own. Recipes already in Mealie are recognised by their source URL.
"""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from .mealie import Mealie
from .scrape import ld_recipe, nutrition_from_ld

log = logging.getLogger(__name__)

SITE = "https://www.bbcgoodfood.com"
SEARCH_API = SITE + "/api/search-frontend/search"
USER_AGENT = "mealie-toolkit (self-hosted recipe manager)"
# Good Food's filter names, passed through as they are; values are its own slugs.
FILTERS = ("mealType", "diet", "ratings", "totalTime", "cuisine", "difficulty")
SORTS = ("rating", "relevant", "published", "quickest")
CACHE_DAYS = 30
WORKERS = 4
_SLUG = re.compile(r"^[a-z0-9-]{1,40}$")


class DiscoverError(RuntimeError):
    pass


def norm_url(url: str) -> str:
    """Host and path only, so http/https, www, query strings and a trailing slash all match."""
    p = urlparse((url or "").strip())
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}".lower()


def is_recipe_url(url: str) -> bool:
    p = urlparse(url or "")
    return p.scheme == "https" and p.netloc == "www.bbcgoodfood.com" and \
        p.path.startswith("/recipes/") and len(p.path) > len("/recipes/")


def clean_params(params: dict) -> dict:
    """Only Good Food's own filter names, with slug values; a short free-text query."""
    out = {}
    q = (params.get("q") or "").strip()[:100]
    if q:
        out["q"] = q
    for name in FILTERS:
        vals = params.get(name) or []
        vals = [vals] if isinstance(vals, str) else vals
        vals = [v for v in vals if _SLUG.match(v)]
        if vals:
            out[name] = vals
    sort = params.get("sort") or "rating"
    out["sort"] = sort if sort in SORTS else "rating"
    return out


def _minutes(iso: str | None) -> int | None:
    """schema.org duration "PT1H10M" -> 70."""
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", (iso or "").strip())
    if not m or not any(m.groups()):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _servings(y) -> int | None:
    for v in y if isinstance(y, list) else [y]:
        hit = re.search(r"\d+", str(v or ""))
        if hit:
            return int(hit.group(0))
    return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def details_from_page(html: str) -> dict:
    ld = ld_recipe(BeautifulSoup(html, "html.parser")) or {}
    n = nutrition_from_ld(ld.get("nutrition") or {})
    rating = ld.get("aggregateRating") or {}
    return {
        "name": ld.get("name") or "",
        "description": re.sub(r"<[^>]+>", "", ld.get("description") or "").strip(),
        "servings": _servings(ld.get("recipeYield")),
        "minutes": _minutes(ld.get("totalTime")),
        "calories": _num(n.get("calories")),
        "sat_fat": _num(n.get("saturatedFatContent")),
        "fat": _num(n.get("fatContent")),
        "sugar": _num(n.get("sugarContent")),
        "fibre": _num(n.get("fiberContent")),
        "protein": _num(n.get("proteinContent")),
        "rating": _num(rating.get("ratingValue")),
        "rating_count": int(_num(rating.get("ratingCount") or rating.get("reviewCount")) or 0),
    }


class GoodFood:
    def __init__(self, data_dir: Path, http: httpx.Client | None = None):
        self.http = http or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30,
                                         follow_redirects=True)
        self.path = Path(data_dir) / "discover-cache.json"
        self.lock = threading.Lock()
        try:
            self.cache: dict[str, dict] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.cache = {}

    def search(self, params: dict, page: int = 1) -> dict:
        query = [(k, v) for k, vs in params.items() for v in (vs if isinstance(vs, list) else [vs])]
        query.append(("page", str(max(1, page))))
        try:
            r = self.http.get(SEARCH_API + "?" + urlencode(query),
                              headers={"Accept": "application/json"})
            r.raise_for_status()
            d = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise DiscoverError(f"Good Food search failed: {type(e).__name__}") from None
        sr = d.get("searchResults") or {}
        items = []
        for it in sr.get("items") or []:
            url = it.get("url") or ""
            if it.get("isPremium") or it.get("postType") != "recipe" or not is_recipe_url(url):
                continue
            rating = it.get("rating") or {}
            items.append({"url": url, "title": it.get("title") or "",
                          "image": (it.get("image") or {}).get("url") or "",
                          "rating": rating.get("ratingValue") or 0,
                          "rating_count": rating.get("ratingCount") or 0,
                          "terms": [t.get("display") for t in it.get("terms") or []
                                    if t.get("display")]})
        return {
            "total": sr.get("totalItems") or 0, "has_more": bool(sr.get("nextUrl")),
            "items": items,
            "filters": [{"name": f["name"], "label": f.get("label") or f["name"],
                         "radio": f.get("type") == "radio",
                         "options": [{"value": o["value"], "label": o.get("label") or o["value"]}
                                     for o in f.get("options") or []]}
                        for f in d.get("filters") or [] if f.get("name") in FILTERS],
            "sorts": [{"value": s["value"], "label": s.get("text") or s["value"]}
                      for s in d.get("sort") or [] if s.get("value") in SORTS],
        }

    def _fresh(self, url: str) -> dict | None:
        hit = self.cache.get(url)
        if not hit:
            return None
        try:
            at = datetime.fromisoformat(hit["at"])
        except (KeyError, ValueError):
            return None
        return hit if datetime.now(timezone.utc) - at < timedelta(days=CACHE_DAYS) else None

    def _fetch(self, url: str) -> dict:
        try:
            r = self.http.get(url)
            r.raise_for_status()
            d = details_from_page(r.text)
        except httpx.HTTPError as e:
            log.warning("discover: %s: %s", url, e)
            return {"error": type(e).__name__}
        d["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return d

    def details(self, urls: list[str]) -> dict[str, dict]:
        out = {u: self._fresh(u) for u in urls}
        todo = [u for u, v in out.items() if v is None]
        if todo:
            with ThreadPoolExecutor(WORKERS) as pool:
                for u, d in zip(todo, pool.map(self._fetch, todo)):
                    out[u] = d
            with self.lock:
                self.cache.update({u: out[u] for u in todo if "error" not in out[u]})
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.cache))
                tmp.replace(self.path)
        return out


def search(mealie: Mealie, gf: GoodFood, params: dict, page: int = 1) -> dict:
    params = clean_params(params)
    res = gf.search(params, page)
    have = {norm_url(r.get("orgURL")) for r in mealie.recipes() if r.get("orgURL")}
    info = gf.details([i["url"] for i in res["items"]])
    results = []
    for it in res["items"]:
        d = info.get(it["url"]) or {}
        results.append({**it, **{k: v for k, v in d.items() if k not in ("at", "rating",
                                                                          "rating_count")},
                        "rating": it["rating"] or d.get("rating") or 0,
                        "rating_count": it["rating_count"] or d.get("rating_count") or 0,
                        "in_mealie": norm_url(it["url"]) in have})
    return {**res, "items": results, "page": page, "params": params}


def import_urls(mealie: Mealie, urls: list[str]) -> dict:
    """Queue these Good Food recipes in Mealie's bulk importer. Anything already in Mealie,
    or not a Good Food recipe URL, is skipped."""
    have = {norm_url(r.get("orgURL")) for r in mealie.recipes() if r.get("orgURL")}
    seen, queue, skipped = set(), [], []
    for u in urls:
        key = norm_url(u)
        if not is_recipe_url(u) or key in have or key in seen:
            skipped.append(u)
            continue
        seen.add(key)
        queue.append(u)
    report = mealie.bulk_import_urls(queue).get("reportId") if queue else None
    return {"queued": len(queue), "skipped": skipped, "report": report}

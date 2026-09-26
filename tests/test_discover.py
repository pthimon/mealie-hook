import json

import httpx

from mealie_toolkit import discover

from fakes import FakeMealie
from test_web import env, post  # noqa: F401  (env is a fixture)

SEARCH = {
    "searchResults": {"totalItems": 1098, "nextUrl": "https://x/page=2", "items": [
        {"title": "Dhal", "url": "https://www.bbcgoodfood.com/recipes/dhal", "isPremium": False,
         "postType": "recipe", "rating": {"ratingValue": 4.7, "ratingCount": 1001},
         "image": {"url": "https://img/dhal.jpg"}, "terms": [{"display": "45 mins"}]},
        {"title": "Paywalled", "url": "https://www.bbcgoodfood.com/premium/x", "isPremium": True,
         "postType": "recipe", "rating": {}},
        {"title": "A collection", "url": "https://www.bbcgoodfood.com/recipes/collection/y",
         "isPremium": False, "postType": "collection", "rating": {}},
        {"title": "Soup", "url": "https://www.bbcgoodfood.com/recipes/soup", "isPremium": False,
         "postType": "recipe", "rating": {"ratingValue": 4.5, "ratingCount": 12}}]},
    "filters": [{"name": "mealType", "label": "Meal type", "options": [{"value": "dinner", "label": "Dinner"}]},
                {"name": "author", "label": "Author", "options": []}],
    "sort": [{"value": "rating", "text": "Most popular"}, {"value": "weird", "text": "?"}],
}
LD = {"@context": "https://schema.org", "@graph": [{"@type": "Recipe", "name": "Dhal",
      "recipeYield": "Serves 4", "totalTime": "PT1H10M",
      "nutrition": {"calories": "397 calories", "saturatedFatContent": "1.5 grams saturated fat",
                    "fatContent": "9 grams fat"},
      "aggregateRating": {"ratingValue": 4.7, "ratingCount": 1001}}]}
PAGE = f'<html><script type="application/ld+json">{json.dumps(LD)}</script></html>'


def client(calls):
    def handler(req: httpx.Request):
        calls.append(str(req.url))
        if "/api/search-frontend/search" in req.url.path:
            return httpx.Response(200, json=SEARCH)
        if req.url.path == "/recipes/soup":
            return httpx.Response(404)
        return httpx.Response(200, text=PAGE)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_drops_premium_and_non_recipes_and_reads_nutrition(tmp_path):
    calls = []
    gf = discover.GoodFood(tmp_path, client(calls))
    m = FakeMealie()
    r = discover.search(m, gf, {"mealType": ["dinner"], "diet": ["healthy"]}, 1)
    assert [i["title"] for i in r["items"]] == ["Dhal", "Soup"]
    dhal = r["items"][0]
    assert (dhal["sat_fat"], dhal["calories"], dhal["servings"], dhal["minutes"]) == (1.5, 397, 4, 70)
    assert dhal["rating_count"] == 1001 and not dhal["in_mealie"]
    assert r["items"][1].get("error") == "HTTPStatusError"         # a dead page is not fatal
    assert r["total"] == 1098 and r["has_more"]
    assert [f["name"] for f in r["filters"]] == ["mealType"]         # only filters we pass on
    assert [s["value"] for s in r["sorts"]] == ["rating"]
    assert "mealType=dinner" in calls[0] and "diet=healthy" in calls[0] and "sort=rating" in calls[0]


def test_pages_are_cached(tmp_path):
    calls = []
    discover.search(FakeMealie(), discover.GoodFood(tmp_path, client(calls)), {}, 1)
    calls2 = []
    discover.search(FakeMealie(), discover.GoodFood(tmp_path, client(calls2)), {}, 1)
    assert sum("/recipes/dhal" in c for c in calls) == 1
    assert not any("/recipes/dhal" in c for c in calls2)             # read from the cache file
    assert any("/recipes/soup" in c for c in calls2)                 # failures are retried


def test_recipes_already_in_mealie_are_flagged(tmp_path):
    m = FakeMealie()
    m.recipes_by_slug["test-stew"]["orgURL"] = "http://bbcgoodfood.com/recipes/dhal/?utm=x"
    r = discover.search(m, discover.GoodFood(tmp_path, client([])), {}, 1)
    assert r["items"][0]["in_mealie"] is True


def test_clean_params_keeps_only_known_filters_and_slugs():
    p = discover.clean_params({"q": "  salmon ", "mealType": ["dinner", "x y"], "author": ["bob"],
                               "sort": "evil", "diet": "healthy"})
    assert p == {"q": "salmon", "mealType": ["dinner"], "diet": ["healthy"], "sort": "rating"}


def test_import_skips_existing_duplicates_and_foreign_urls():
    m = FakeMealie()
    m.recipes_by_slug["test-stew"]["orgURL"] = "https://www.bbcgoodfood.com/recipes/have"
    res = discover.import_urls(m, ["https://www.bbcgoodfood.com/recipes/new",
                                   "https://www.bbcgoodfood.com/recipes/new/",
                                   "https://www.bbcgoodfood.com/recipes/have",
                                   "https://evil.example/recipes/x",
                                   "https://www.bbcgoodfood.com/premium/y"])
    assert res["queued"] == 1 and res["report"] == "rep-1" and len(res["skipped"]) == 4
    assert m.bulk_imported == [["https://www.bbcgoodfood.com/recipes/new"]]


def test_minutes_and_servings_parsing():
    assert discover._minutes("PT45M") == 45 and discover._minutes("PT2H") == 120
    assert discover._minutes("") is None and discover._servings(["Makes 12", "12"]) == 12


def test_discover_endpoints(env, tmp_path):  # noqa: F811
    from fastapi.testclient import TestClient
    from mealie_toolkit.web import create_app
    from test_web import ADMIN, auth_as
    gf = discover.GoodFood(tmp_path, client([]))
    c = TestClient(create_app(env["pipe"], env["worker"], auth=auth_as(ADMIN), goodfood=gf))
    r = c.get("/ui/api/discover", params=[("mealType", "dinner"), ("diet", "healthy"), ("diet", "vegan")]).json()
    assert r["params"]["diet"] == ["healthy", "vegan"] and r["items"][0]["title"] == "Dhal"
    res = post(c, "/ui/api/discover/import", {"urls": ["https://www.bbcgoodfood.com/recipes/dhal"]}).json()
    assert res["queued"] == 1 and env["mealie"].bulk_imported == [["https://www.bbcgoodfood.com/recipes/dhal"]]

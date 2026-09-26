import json

from mealie_hook import planner
from mealie_hook.state import State

from fakes import FakeMealie
from test_web import ADMIN, env, post  # noqa: F401  (env is a fixture)


def ing(ref, qty, food, unit=None, title=None, on_hand=False, sub=None):
    return {"referenceId": ref, "quantity": qty, "note": "", "title": title,
            "originalText": f"{qty} {food}", "display": f"{qty} {food}",
            "unit": {"name": unit} if unit else None,
            "food": {"name": food, "householdsWithIngredientFood": ["family"] if on_hand else []},
            "referencedRecipe": sub}


def recipe(slug, servings=0, yq=0, yield_text="", ings=()):
    return {"id": f"id-{slug}", "slug": slug, "name": slug.title(), "recipeServings": servings,
            "recipeYieldQuantity": yq, "recipeYield": yield_text, "recipeIngredient": list(ings)}


def mealie_with_plan():
    m = FakeMealie()
    m.recipes_by_slug = {
        "stew": recipe("stew", servings=4, ings=[
            ing("s1", 2, "carrot", title="For the stew"), ing("s2", 400, "passata", "g"),
            ing("s3", 1, "salt", on_hand=True),
            ing("s4", 1, "dressing", sub={"id": "id-dressing", "slug": "dressing"})]),
        "dressing": recipe("dressing", servings=2, ings=[ing("d1", 2, "tahini", "tbsp")]),
        "cookies": recipe("cookies", yq=25, yield_text="items", ings=[ing("c1", 100, "raisins", "g")]),
        "mystery": recipe("mystery", ings=[ing("x1", 1, "egg")]),
    }
    m.plan = [
        {"id": 3, "date": "2026-09-28", "entryType": "snack", "recipe": {"slug": "cookies"}},
        {"id": 1, "date": "2026-09-28", "entryType": "dinner", "recipe": {"slug": "stew"}},
        {"id": 2, "date": "2026-09-27", "entryType": "lunch", "recipe": {"slug": "mystery"}},
        {"id": 4, "date": "2026-09-29", "entryType": "dinner", "title": "Out", "text": ""},
        {"id": 9, "date": "2026-10-20", "entryType": "dinner", "recipe": {"slug": "stew"}},
    ]
    return m


def test_build_defaults_and_order(tmp_path):
    v = planner.build(mealie_with_plan(), State(tmp_path), "2026-09-26", "2026-10-02", 2)
    es = v["entries"]
    assert [e["id"] for e in es] == [2, 1, 3, 4]              # by date, then meal order
    by = {e["id"]: e for e in es}
    assert by[1]["amount"] == 2 and by[1]["recipe"]["base"]["kind"] == "servings"
    assert by[3]["amount"] == 25 and by[3]["recipe"]["base"]["unit"] == "items"   # whole batch
    assert by[2]["amount"] == 1 and by[2]["recipe"]["base"]["kind"] == "batch"
    assert "recipe" not in by[4]
    stew = by[1]["recipe"]
    assert stew["url"] == "/g/home/r/stew"
    [sect] = stew["sections"]
    assert sect["title"] == "For the stew" and [r["ref"] for r in sect["rows"]] == ["s1", "s2", "s3"]
    assert [r["checked"] for r in sect["rows"]] == [True, True, False]   # salt is on hand
    [sub] = stew["subs"]
    assert sub["slug"] == "dressing" and sub["factor"] == 1


def test_add_posts_scales_and_remembers(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    res = planner.add(m, st, "L1",
                      [{"slug": "stew", "scale": 0.5, "refs": ["s1", "s2", "s4"]},
                       {"slug": "dressing", "scale": 0.5, "refs": ["d1"]},
                       {"slug": "cookies", "scale": 1, "refs": []}],
                      [{"id": 1, "date": "2026-09-28", "slug": "stew", "scale": 0.5}])
    assert res == {"recipes": 2, "ingredients": 3, "missing": 0}
    [(list_id, body)] = m.added_to_list
    assert list_id == "L1"
    assert [(b["recipeId"], b["recipeIncrementQuantity"]) for b in body] == \
        [("id-stew", 0.5), ("id-dressing", 0.5)]
    # the sub-recipe row itself is never sent: the sub-recipe goes as its own item
    assert [i["referenceId"] for i in body[0]["recipeIngredients"]] == ["s1", "s2"]
    v = planner.build(m, st, "2026-09-26", "2026-10-02", 2)
    stew = next(e for e in v["entries"] if e["id"] == 1)
    assert stew["added"] and stew["remembered"] == 0.5 and stew["amount"] == 2.0   # 0.5 x 4


def test_remembered_whole_batch_beats_default(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    st.plan_added([{"id": 77, "date": "2026-09-01", "slug": "stew", "scale": 1}])
    v = planner.build(m, st, "2026-09-26", "2026-10-02", 2)
    assert next(e for e in v["entries"] if e["id"] == 1)["amount"] == 4


def test_add_rejects_silly_scales_and_counts_missing(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    try:
        planner.add(m, st, "L1", [{"slug": "stew", "scale": 0, "refs": ["s1"]}], [])
        assert False, "zero scale accepted"
    except ValueError:
        pass
    res = planner.add(m, st, "L1", [{"slug": "stew", "scale": 1, "refs": ["s1", "gone"]}], [])
    assert res["missing"] == 1 and res["ingredients"] == 1


def test_old_added_entries_are_forgotten(tmp_path):
    st = State(tmp_path)
    st.plan_added([{"id": 1, "date": "2020-01-01"}, {"id": 2, "date": "2999-01-01"}])
    assert set(st.data["plan_added"]) == {"2"}


def test_sub_recipe_cycles_stop(tmp_path):
    m = FakeMealie()
    m.recipes_by_slug = {
        "a": recipe("a", servings=2, ings=[ing("a1", 1, "b", sub={"slug": "b"})]),
        "b": recipe("b", servings=2, ings=[ing("b1", 1, "a", sub={"slug": "a"}), ing("b2", 1, "egg")]),
    }
    m.plan = [{"id": 1, "date": "2026-09-28", "entryType": "dinner", "recipe": {"slug": "a"}}]
    [e] = planner.build(m, State(tmp_path), "2026-09-28", "2026-09-28", 2)["entries"]
    assert e["recipe"]["subs"][0]["slug"] == "b" and e["recipe"]["subs"][0]["subs"] == []


def test_plan_endpoints(env):  # noqa: F811
    m = mealie_with_plan()
    env["pipe"].mealie = m
    c = env["client"]()
    v = c.get("/ui/api/plan", params={"start": "2026-09-26", "end": "2026-10-02"}).json()
    assert [e["id"] for e in v["entries"]] == [2, 1, 3, 4] and v["lists"][0]["id"] == "L1"
    r = post(c, "/ui/api/plan/add", {"list_id": "L1",
             "items": [{"slug": "cookies", "scale": 1, "refs": ["c1"]}],
             "entries": [{"id": 3, "date": "2026-09-28", "slug": "cookies", "scale": 1}]}).json()
    assert r["recipes"] == 1
    bad = post(c, "/ui/api/plan/add", {"list_id": "L1",
               "items": [{"slug": "cookies", "scale": 99, "refs": ["c1"]}]})
    assert bad.status_code == 422

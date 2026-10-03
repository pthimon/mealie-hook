from datetime import date

from mealie_toolkit import planner, upcoming
from mealie_toolkit.state import State

from test_planner import ing, mealie_with_plan, recipe
from test_web import env  # noqa: F401  (env is a fixture)

DAY = date(2026, 9, 28)


def foods_by_name(v):
    return {g["food"] or g["text"]: g for g in v["foods"]}


def test_sizes_come_from_the_add_then_last_used_then_default(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    # the stew (entry 1) was added at x0.75; the cookies only have a last-used size
    planner.add(m, st, "L1", [], [{"id": 1, "date": "2026-09-28", "slug": "stew", "scale": 0.75}])
    st.data["plan_scales"]["cookies"] = 2
    v = upcoming.build(m, st, DAY, 2)
    by = {x["id"]: x for x in v["meals"]}
    assert (by[1]["scale"], by[1]["source"], by[1]["servings"]) == (0.75, "added", 3)
    assert (by[3]["scale"], by[3]["source"]) == (2, "last used")
    assert by[2]["passed"] and not by[1]["passed"]       # the 27th is before "today"


def test_older_add_records_fall_back_to_the_recipe_scale(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    st.data["plan_added"] = {"1": {"at": "x", "date": "2026-09-28"}}     # no per-meal scale
    st.data["plan_scales"] = {"stew": 0.5}
    [stew] = [x for x in upcoming.build(m, st, DAY, 2)["meals"] if x["id"] == 1]
    assert (stew["scale"], stew["source"]) == (0.5, "added")


def test_amounts_add_up_across_meals_and_sub_recipes(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    m.recipes_by_slug["cookies"]["recipeIngredient"].append(ing("c2", 2, "carrot", title=None))
    m.recipes_by_slug["dressing"]["recipeIngredient"].append(ing("d2", 4, "carrot"))
    v = upcoming.build(m, st, DAY, 2, days=7)            # stew at 2 of 4 servings: x0.5
    carrot = foods_by_name(v)["carrot"]
    # stew 2 x0.5 = 1, its dressing 4 x0.5 = 2, cookies 2 x1 = 2
    assert carrot["amounts"] == {"": 5} and carrot["first"] == "2026-09-28"
    assert [(u["name"], u["qty"]) for u in carrot["uses"]] == \
        [("Stew", 1), ("Stew (Dressing)", 2), ("Cookies", 2)]


def test_unit_spellings_merge(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    m.recipes_by_slug["stew"]["recipeIngredient"].append(ing("s9", 2, "garlic", "cloves"))
    m.recipes_by_slug["cookies"]["recipeIngredient"].append(ing("c9", 1, "garlic", "clove"))
    garlic = foods_by_name(upcoming.build(m, st, DAY, 2, days=7))["garlic"]
    assert garlic["amounts"] == {"clove": 2}             # 2 x0.5 + 1


def test_the_window_counts_a_meal_planned_twice(tmp_path):
    v = upcoming.build(mealie_with_plan(), State(tmp_path), DAY, 2)   # stew on the 28th and 20 Oct
    assert foods_by_name(v)["passata"]["amounts"] == {"g": 400}


def test_a_food_nothing_needs_is_absent(tmp_path):
    assert "leek" not in foods_by_name(upcoming.build(mealie_with_plan(), State(tmp_path), DAY, 2))


def test_plan_add_records_each_meals_scale(tmp_path):
    st = State(tmp_path)
    planner.add(mealie_with_plan(), st, "L1", [], [{"id": 7, "date": "2026-09-28", "slug": "stew", "scale": 0.5}])
    assert st.data["plan_added"]["7"]["scale"] == 0.5 and st.data["plan_added"]["7"]["slug"] == "stew"


def test_upcoming_endpoint(env):  # noqa: F811
    env["pipe"].mealie = mealie_with_plan()
    c = env["client"]()
    v = c.get("/ui/api/upcoming", params={"today": "2026-09-28"}).json()
    assert v["today"] == "2026-09-28" and [x["id"] for x in v["meals"]][:3] == [2, 1, 3]
    assert [x["passed"] for x in v["meals"]][:2] == [True, False]
    v = c.get("/ui/api/upcoming", params={"today": "2026-09-28", "since": "2026-09-28"}).json()
    assert v["since_chosen"] and [x["id"] for x in v["meals"]][:1] == [1]
    assert c.get("/ui/api/upcoming", params={"today": "nonsense"}).status_code == 422
    assert c.get("/ui/api/upcoming", params={"since": "soon"}).status_code == 422


def dates(m):
    return {e["id"]: e["date"] for e in m.plan}


def test_move_changes_only_the_date():
    m = mealie_with_plan()
    res = upcoming.move(m, 1, date(2026, 9, 30))
    assert res == {"id": 1, "from": "2026-09-28", "to": "2026-09-30"}
    [(eid, body)] = m.plan_updates
    assert eid == 1 and body["date"] == "2026-09-30" and body["entryType"] == "dinner"
    assert body["recipeId"] == "rid" and body["userId"] == "u"     # the rest goes back as it was


def test_move_to_the_same_day_writes_nothing():
    m = mealie_with_plan()
    upcoming.move(m, 1, date(2026, 9, 28))
    assert not getattr(m, "plan_updates", [])


def test_swap_trades_days():
    m = mealie_with_plan()
    res = upcoming.swap(m, 2, 3)                   # 27th lunch <-> 28th snack
    assert res["changed"] and dates(m)[2] == "2026-09-28" and dates(m)[3] == "2026-09-27"


def test_swap_on_the_same_day_is_a_no_op():
    m = mealie_with_plan()
    assert upcoming.swap(m, 1, 3)["changed"] is False and not getattr(m, "plan_updates", [])


def test_failed_swap_puts_the_first_meal_back():
    import pytest
    from mealie_toolkit.mealie import MealieError
    m = mealie_with_plan()
    m.fail_update_of = 3
    with pytest.raises(MealieError):
        upcoming.swap(m, 2, 3)
    assert dates(m)[2] == "2026-09-27" and dates(m)[3] == "2026-09-28"   # as before


def test_move_and_swap_endpoints(env):  # noqa: F811
    from test_web import post
    m = mealie_with_plan()
    env["pipe"].mealie = m
    c = env["client"]()
    assert post(c, "/ui/api/upcoming/move", {"id": 1, "date": "2026-10-01"}).json()["to"] == "2026-10-01"
    assert post(c, "/ui/api/upcoming/move", {"id": 1, "date": "soon"}).status_code == 422
    assert post(c, "/ui/api/upcoming/swap", {"a": 1, "b": 2}).json()["changed"]
    m.fail_update_of = 3
    r = post(c, "/ui/api/upcoming/swap", {"a": 2, "b": 3})
    assert r.status_code == 502 and "Mealie refused" in r.json()["detail"]


def test_passed_meals_since_the_last_shop_are_listed_but_not_counted(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    st.record_bought({"egg": "2026-09-26T19:00:00Z"})          # the last shop: the 26th
    v = upcoming.build(m, st, DAY, 2, days=7)
    assert v["since"] == "2026-09-26" and v["last_shop"] == "2026-09-26" and not v["since_chosen"]
    passed = [x for x in v["meals"] if x["passed"]]
    assert [x["id"] for x in passed] == [2]                    # Mystery on the 27th
    assert "egg" not in foods_by_name(v)                       # only Mystery uses egg


def test_last_shop_is_the_latest_tick_seen(tmp_path):
    st = State(tmp_path)
    assert upcoming.last_shop(st) is None
    st.record_bought({"egg": "2026-09-20T10:00:00Z", "milk": "2026-09-24T18:00:00Z"})
    st.data["last_tick"] = "2026-09-22T09:00:00+00:00"
    assert upcoming.last_shop(st) == date(2026, 9, 24)


def test_without_a_shop_passed_looks_back_a_week(tmp_path):
    v = upcoming.build(mealie_with_plan(), State(tmp_path), DAY, 2, days=7)
    assert v["since"] == "2026-09-21" and v["last_shop"] is None


def test_a_chosen_start_after_today_is_clamped(tmp_path):
    v = upcoming.build(mealie_with_plan(), State(tmp_path), DAY, 2, days=7, since=date(2026, 10, 9))
    assert v["since"] == "2026-09-28"


def test_a_shop_ticked_off_in_mealie_just_now_counts(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    m.shop_items = [{"id": "t", "checked": True, "updatedAt": "2026-09-27T20:00:00", "food": {"name": "egg"}}]
    v = upcoming.build(m, st, DAY, 2, days=7)
    assert v["last_shop"] == "2026-09-27" and v["since"] == "2026-09-27"

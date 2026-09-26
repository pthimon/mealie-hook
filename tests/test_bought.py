from mealie_toolkit import bought, planner
from mealie_toolkit.shopping import tick
from mealie_toolkit.state import State

from fakes import FakeMealie
from test_planner import mealie_with_plan
from test_web import FakeHA


def ticked_item(id_, food, at, plural=None):
    return {"id": id_, "checked": True, "quantity": 1, "updatedAt": at,
            "food": {"name": food, "pluralName": plural}}


def test_harvest_takes_the_latest_tick_per_food(tmp_path):
    m, st = FakeMealie(), State(tmp_path)
    m.shop_items = [ticked_item("a", "Garlic", "2026-09-09T19:39:00"),
                    ticked_item("b", "garlic", "2026-09-23T20:23:00"),
                    ticked_item("c", "lemon", "2026-09-16T19:48:00"),
                    {"id": "d", "checked": False, "updatedAt": "2026-09-25T10:00:00",
                     "food": {"name": "carrot"}}]                     # still to buy: not bought
    rec = bought.harvest(m, st)
    assert rec == {"garlic": "2026-09-23T20:23:00+00:00", "lemon": "2026-09-16T19:48:00+00:00"}


def test_record_survives_mealie_forgetting(tmp_path):
    m, st = FakeMealie(), State(tmp_path)
    m.shop_items = [ticked_item("a", "garlic", "2026-09-23T20:23:00")]
    bought.harvest(m, st)
    m.shop_items = []                                # "delete checked items" in Mealie
    assert bought.harvest(m, State(tmp_path))["garlic"].startswith("2026-09-23")


def test_older_tick_never_overwrites_newer(tmp_path):
    st = State(tmp_path)
    st.record_bought({"garlic": "2026-09-23T20:23:00Z"})
    st.record_bought({"garlic": "2026-09-01T10:00:00+01:00"})
    assert st.data["bought"]["garlic"] == "2026-09-23T20:23:00+00:00"


def test_lookup_by_name_or_plural_and_punctuation():
    rec = {"carrots": "2026-09-20T00:00:00+00:00", "flat leaf parsley": "2026-09-21T00:00:00+00:00"}
    assert bought.last_bought(rec, "carrot", "carrots") == "2026-09-20T00:00:00+00:00"
    assert bought.last_bought(rec, "flat-leaf parsley") == "2026-09-21T00:00:00+00:00"
    assert bought.last_bought(rec, "leek") is None


def test_shopping_tick_records_mealie_and_ha(tmp_path):
    m, st = FakeMealie(), State(tmp_path)
    ha = FakeHA([{"id": "h1", "name": "Carrots", "complete": False}])
    tick(m, ha, "L1", ["m2"], ["h1"], st)
    assert set(st.data["bought"]) == {"passata", "carrots"}


def test_plan_rows_carry_last_bought(tmp_path):
    m, st = mealie_with_plan(), State(tmp_path)
    m.shop_items = [ticked_item("a", "carrot", "2026-09-23T20:23:00")]
    v = planner.build(m, st, "2026-09-26", "2026-10-02", 2)
    stew = next(e for e in v["entries"] if e["id"] == 1)["recipe"]
    rows = {r["food"]: r["bought"] for r in stew["sections"][0]["rows"]}
    assert rows["carrot"] == "2026-09-23T20:23:00+00:00" and rows["passata"] is None

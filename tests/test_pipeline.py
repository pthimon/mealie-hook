import copy
import json

import pytest

from mealie_toolkit import MARKER_KEY
from mealie_toolkit.config import ROOT, Config
from mealie_toolkit.pipeline import REVIEW_NOTE_TITLE, Pipeline
from mealie_toolkit.rules import RulesStore
from mealie_toolkit.server import Worker, handle_event, parse_document_data
from mealie_toolkit.state import State

from fakes import FakeLLM, FakeMealie, base_recipe, raw


GOOD = dict(
    IngredientReply={"rows": [
        {"i": 0, "kind": "ingredient", "quantity": 2, "unit": None, "food": "carrots", "note": ""},
        {"i": 1, "kind": "heading", "quantity": 0, "unit": None, "food": "", "note": ""},
        {"i": 2, "kind": "ingredient", "quantity": 1, "unit": None, "food": "parsnip", "note": ""},
    ]},
    ClassificationReply={"dish": "A winter veg stew.", "category": "Dinner",
                         "tags": ["Vegetables", "Winter"], "tools": []},
    LabelReply={"foods": [{"food": "parsnip", "label": "Fruit & Veg", "countable": True,
                           "plural": "parsnips"}]},
)


@pytest.fixture
def make(tmp_path):
    def _make(llm_script=GOOD, **cfg):
        m = FakeMealie()
        c = Config(data_dir=tmp_path, process_since="2029-01-01T00:00:00Z", **cfg)
        return Pipeline(c, m, FakeLLM(**llm_script), State(tmp_path),
                        RulesStore(tmp_path / "rules", ROOT / "rules")), m
    return _make


def test_happy_path_writes_once(make):
    pipe, m = make()
    res = pipe.process("test-stew")
    assert res.status == "written" and res.flags == []
    assert len(m.puts) == 1
    r = m.puts[0]
    assert [i["food"]["name"] for i in r["recipeIngredient"]] == ["carrot", "parsnip"]
    assert r["recipeIngredient"][1]["title"] == "For the topping"
    assert r["recipeIngredient"][0]["food"]["id"] == "f1"          # existing record reused
    assert m.created_foods == [{"id": "new-parsnip", "name": "parsnip",
                                "pluralName": "parsnips", "labelId": "l1"}]
    assert r["recipeIngredient"][1]["food"]["id"] == "new-parsnip"
    assert [c["name"] for c in r["recipeCategory"]] == ["Dinner"]
    assert {t["name"] for t in r["tags"]} == {"Vegetables", "Winter"}
    assert r["settings"]["showNutrition"] is True
    assert MARKER_KEY in r["extras"]
    assert pipe.state.latest_snapshot("test-stew") is not None


def test_new_units_are_created_with_their_plural(make):
    script = copy.deepcopy(GOOD)
    script["IngredientReply"]["rows"][0].update(quantity=2, unit="tins", food="carrots")
    script["IngredientReply"]["rows"][2].update(quantity=10, unit="g", food="parsnip")
    pipe, m = make(script)
    m._units = []                                       # neither unit exists yet
    pipe.process("test-stew")
    assert sorted((u["name"], u["pluralName"]) for u in m.created_units) == \
        [("g", None), ("tin", "tins")]                  # abbreviations get no plural


def test_unit_plurals_table():
    from mealie_toolkit.foods import ALLOWED_UNITS, UNIT_PLURALS
    assert UNIT_PLURALS["clove"] == "cloves" and UNIT_PLURALS["bunch"] == "bunches"
    assert UNIT_PLURALS["punnet"] == "punnets" and UNIT_PLURALS["pinch"] == "pinches"
    assert not {"g", "kg", "ml", "l", "tsp", "tbsp", "dsp"} & set(UNIT_PLURALS)
    assert set(UNIT_PLURALS) == ALLOWED_UNITS - {"g", "kg", "ml", "l", "tsp", "tbsp", "dsp"}


def test_dry_run_writes_nothing(make):
    pipe, m = make()
    res = pipe.process("test-stew", dry=True)
    assert res.status == "dry-run" and m.puts == [] and m.created_foods == []
    assert any("parsnip (NEW)" in p for p in res.preview)


def test_marker_skips_second_run(make):
    pipe, m = make()
    pipe.process("test-stew")
    assert pipe.process("test-stew").status == "skipped"
    assert len(m.puts) == 1


def test_edit_during_processing_defers(make):
    pipe, m = make()
    m.edit_during_processing = True
    res = pipe.process("test-stew")
    assert res.status == "deferred" and m.puts == [] and m.created_foods == []


def test_flags_add_review_tag_and_note(make):
    script = dict(GOOD)
    script["IngredientReply"] = {"rows": [
        {"i": 0, "kind": "ingredient", "quantity": 2, "unit": None, "food": "carrots", "note": ""},
        {"i": 1, "kind": "heading", "quantity": 0, "unit": None, "food": "", "note": ""},
        {"i": 2, "kind": "ingredient", "quantity": 1, "unit": None,
         "food": "parsnip, finely chopped", "note": ""},
    ]}
    pipe, m = make(script)
    res = pipe.process("test-stew")
    assert res.status == "written" and res.flags
    r = m.puts[0]
    assert "Needs review" in {t["name"] for t in r["tags"]}
    assert r["notes"][-1]["title"] == REVIEW_NOTE_TITLE
    assert r["recipeIngredient"][1]["food"] is None                # left as written


def test_provenance_tag_not_offered_to_model(make):
    pipe, m = make()
    pipe.process("test-stew", dry=True)
    # the schema would have rejected it; check the rule path too
    assert "Food for Life Cookbook" not in pipe.process("test-stew", dry=True).tags


def test_sweep_only_takes_new_scraped_recipes(make):
    pipe, m = make()
    old = base_recipe() | {"slug": "old", "createdAt": "2020-01-01T00:00:00Z"}
    manual = base_recipe() | {"slug": "manual", "orgURL": None}
    m.recipes_by_slug.update(old=old, manual=manual)
    assert pipe.candidates() == ["test-stew"]
    results = pipe.sweep()
    assert [r.slug for r in results] == ["test-stew"]
    assert pipe.candidates() == []                                  # remembered as done


def test_failures_give_up_after_max_attempts(make):
    pipe, m = make(llm_script={}, max_attempts=2)                    # every model call KeyErrors
    pipe.sweep()
    assert m.puts == []
    pipe.sweep()
    r = m.puts[-1]
    assert "failed" in r["extras"][MARKER_KEY]
    assert "Needs review" in {t["name"] for t in r["tags"]}
    assert pipe.candidates() == []


# ------------------------------------------------------------------ event handling

class Recorder(Worker):
    def __init__(self):
        self.triggers = []

    def trigger(self, reason, report_id=None):
        self.triggers.append((reason, report_id))


def apprise(event_type, doc):
    return json.dumps({"title": "Recipe Created", "message": "x", "type": "info",
                       "event_type": event_type, "document_data": json.dumps(doc)}).encode()


def test_single_import_event_triggers():
    w = Recorder()
    handle_event(w, apprise("recipe_created", {"recipe_slug": "soup", "operation": "create"}))
    assert w.triggers == [("event", None)]


def test_bulk_import_event_carries_report():
    w = Recorder()
    handle_event(w, apprise("recipe_created", {"report_id": "abc", "operation": "create"}))
    assert w.triggers == [("event", "abc")]


@pytest.mark.parametrize("body", [apprise("recipe_updated", {"recipe_slug": "soup"}),
                                  b"not json", b""])
def test_other_payloads_ignored(body):
    w = Recorder()
    handle_event(w, body)
    assert w.triggers == []


def test_apprise_plus_encoded_document_data():
    # captured verbatim from Mealie v3.22 + Apprise 1.12 on a bulk import
    body = (b'{"version": "1.0", "title": "Recipe Created", "message": "generic", '
            b'"attachments": [], "type": "info", "event_type": "recipe_created", '
            b'"integration_id": "generic", "document_data": "{\\"documentType\\":+'
            b'\\"recipe_bulk_report\\",+\\"operation\\":+\\"create\\",+'
            b'\\"reportId\\":+\\"26205be3-e8bc-43ee-8b50-849447fde889\\"}", '
            b'"event_id": "cf6a76f4", "timestamp": "2026-09-26T10:59:40+00:00"}')
    w = Recorder()
    handle_event(w, body)
    assert w.triggers == [("event", "26205be3-e8bc-43ee-8b50-849447fde889")]


def test_plain_json_document_data_still_parses():
    assert parse_document_data('{"recipeSlug": "soup"}') == {"recipeSlug": "soup"}
    assert parse_document_data("garbage") == {}

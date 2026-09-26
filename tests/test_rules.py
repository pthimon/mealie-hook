import pytest

from mealie_toolkit.config import ROOT
from mealie_toolkit.rules import (FILES, VOCAB_FILE, Rules, RulesError, RulesStore, Vocabulary)

LIVE = {"categories": ["Dinner", "Lunch", "Breakfast", "Dessert", "Snack", "Side"],
        "tags": ["Chicken", "Summer", "Winter", "Savoury", "Food for Life Cookbook", "Needs review",
                 "Duck"],
        "tools": ["Slow Cooker"], "labels": ["Herbs & Spices", "Fish"]}


@pytest.fixture
def store(tmp_path):
    return RulesStore(tmp_path / "rules", ROOT / "rules")


def test_seeded_from_defaults_and_valid(store):
    rules = store.load()
    assert set(rules.texts) == set(FILES)
    assert rules.vocab.with_role("tags", "winter") == ["Winter"]
    assert rules.vocab.with_role("categories", "main") == ["Dinner", "Lunch"]


def test_toml_round_trip_keeps_everything(store):
    v = store.load().vocab
    assert Vocabulary.parse_toml(v.to_toml()) == v


def test_role_must_suit_its_section():
    with pytest.raises(ValueError):
        Vocabulary.model_validate({"tags": {"Dinner": {"roles": ["main"]}}})


def test_render_classify(store):
    text = store.load().render("classify.md", LIVE, hidden_tags={"Needs review"})
    assert "{{" not in text
    assert "- **Dinner** — Evening mains" in text
    assert "Protein tags:" in text and "- **Chicken**" in text
    assert "- **Savoury** (not on mains)" in text
    assert "Food for Life Cookbook" not in text          # provenance never offered
    assert "Needs review" not in text
    assert "Other tags:\n- **Duck**" in text             # live, but no guidance yet


def test_placeholder_removal_rejected(store):
    texts = store.texts()
    texts["classify.md"] = texts["classify.md"].replace("{{tags}}", "")
    with pytest.raises(RulesError, match="placeholder"):
        Rules.from_texts(texts)


def test_drift_reports_all_three_kinds(store):
    live = dict(LIVE, tools=[])                           # Slow Cooker gone from Mealie
    drift = store.load().drift(live)
    msgs = [d["message"] for d in drift]
    assert any("'Slow Cooker' is in the vocabulary but not in Mealie" in m for m in msgs)
    assert any("'Duck' has no guidance" in m for m in msgs)
    assert any("slow-cooker ⇒ winter" in m and "disabled" in m for m in msgs)
    creatable = [d for d in drift if d.get("create")]
    assert {"kind": "tools", "name": "Slow Cooker"} in [{"kind": d["kind"], "name": d["name"]}
                                                       for d in creatable]


def test_save_snapshots_and_revert(store):
    original = store.texts()["labels.md"]
    snap = store.save({"labels.md": original + "\nExtra line.\n"}, "add a line", "cook")
    assert "Extra line." in store.texts()["labels.md"]
    [h] = store.list_history()
    assert h["id"] == snap and h["summary"] == "add a line" and h["who"] == "cook"
    store.revert(snap)
    assert store.texts()["labels.md"] == original
    assert len(store.list_history()) == 2                 # the revert is itself undoable


def test_invalid_save_writes_nothing(store):
    before = store.texts()
    with pytest.raises(RulesError):
        store.save({VOCAB_FILE: "not = [valid"}, "broken")
    assert store.texts() == before and store.list_history() == []


def test_version_changes_with_content(store):
    v1 = store.version()
    store.save({"labels.md": store.texts()["labels.md"] + "\n"}, "x")
    assert store.version() != v1


def test_bad_snapshot_id_rejected(store):
    with pytest.raises(RulesError):
        store.snapshot_texts("../../etc")


def test_shipped_default_is_canonical():
    text = (ROOT / "rules" / VOCAB_FILE).read_text()
    assert Vocabulary.parse_toml(text).to_toml() == text


def test_names_needing_quotes_round_trip():
    v = Vocabulary.model_validate({"labels": {"Nuts, Seeds & Dried Fruit": {"guidance": 'say "hi"\nok'}},
                                   "tags": {"za’atar": {}}})
    assert Vocabulary.parse_toml(v.to_toml()) == v

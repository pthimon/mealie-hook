import pytest

from mealie_toolkit.chat import (EditProposal, ProposalStore, apply_proposal, history_line, propose,
                              suggested_creates)
from mealie_toolkit.config import ROOT
from mealie_toolkit.rules import VOCAB_FILE, RulesStore, Vocabulary


@pytest.fixture
def texts(tmp_path):
    return RulesStore(tmp_path / "rules", ROOT / "rules").texts()


def P(**kw):
    return EditProposal(reply="ok", **kw)


def test_vocab_set_adds_new_tag(texts):
    a = apply_proposal(texts, P(vocab_ops=[{"op": "set", "kind": "tags", "name": "Duck",
                                             "roles": ["protein"]}]))
    assert not a.errors and a.changed == [VOCAB_FILE]
    assert Vocabulary.parse_toml(a.texts[VOCAB_FILE]).tags["Duck"].roles == ["protein"]


def test_vocab_set_keeps_unmentioned_fields(texts):
    a = apply_proposal(texts, P(vocab_ops=[{"op": "set", "kind": "tags", "name": "Winter",
                                             "guidance": "Only stews."}]))
    w = Vocabulary.parse_toml(a.texts[VOCAB_FILE]).tags["Winter"]
    assert w.guidance == "Only stews." and w.roles == ["season", "winter"]


def test_wrong_role_for_section_is_an_error(texts):
    a = apply_proposal(texts, P(vocab_ops=[{"op": "set", "kind": "tools", "name": "Wok",
                                             "roles": ["protein"]}]))
    assert a.errors


def test_delete_missing_is_an_error(texts):
    a = apply_proposal(texts, P(vocab_ops=[{"op": "delete", "kind": "tags", "name": "Nope"}]))
    assert a.errors


def test_prompt_edit_must_match_once(texts):
    ok = apply_proposal(texts, P(prompt_edits=[{"file": "labels.md",
                                                "find": "Choose the aisle you would find it in, not its botanical truth.",
                                                "replace": "Choose the aisle you would find it in."}]))
    assert not ok.errors and ok.changed == ["labels.md"]
    missing = apply_proposal(texts, P(prompt_edits=[{"file": "labels.md", "find": "not there",
                                                     "replace": "x"}]))
    assert "found 0 times" in missing.errors[0]
    many = apply_proposal(texts, P(prompt_edits=[{"file": "labels.md", "find": "the", "replace": "a"}]))
    assert many.errors


def test_removing_placeholder_is_an_error(texts):
    a = apply_proposal(texts, P(prompt_edits=[{"file": "classify.md", "find": "{{tools}}",
                                               "replace": ""}]))
    assert any("placeholder" in e for e in a.errors)


def test_suggested_creates_include_vocab_names_missing_from_mealie():
    p = P(vocab_ops=[{"op": "set", "kind": "tags", "name": "Duck", "roles": ["protein"]},
                     {"op": "set", "kind": "tags", "name": "Chicken", "guidance": "x"}])
    assert suggested_creates(p, {"tags": ["Chicken"]}) == [{"kind": "tags", "name": "Duck"}]


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def structured(self, messages, reply, name, **_):
        self.seen.append(messages)
        return reply.model_validate(self.replies.pop(0))


def test_propose_retries_once_with_the_errors(texts):
    bad = {"reply": "done", "prompt_edits": [{"file": "labels.md", "find": "nope", "replace": "x"}]}
    good = {"reply": "done", "vocab_ops": [{"op": "set", "kind": "labels", "name": "Fish",
                                            "guidance": "Fresh fish."}]}
    llm = ScriptedLLM(bad, good)
    p, a = propose(llm, texts, {"labels": ["Fish"]}, [{"role": "user", "content": "fish rule"}])
    assert not a.errors and p.vocab_ops[0].name == "Fish"
    assert "has problems" in llm.seen[1][-1]["content"]
    assert "## Current labels.md" in llm.seen[0][0]["content"]


def test_store_and_history_line():
    s = ProposalStore(keep=2)
    ids = [s.add(base_version="v", request="r", proposal=P(), applied=None, creates=[]).id
           for _ in range(3)]
    assert s.get(ids[0]) is None and s.get(ids[2]) is not None
    p = s.add(base_version="v", request="r", applied=None, creates=[],
              proposal=P(vocab_ops=[{"op": "set", "kind": "tags", "name": "Duck"}]))
    assert history_line(p) == "ok\n[proposed: set tags.Duck]"


def test_guidance_change_is_a_one_line_diff(texts):
    from mealie_toolkit.rules import unified
    a = apply_proposal(texts, P(vocab_ops=[{"op": "set", "kind": "categories", "name": "Dinner",
                                             "guidance": "Evening mains."}]))
    diff = unified(texts[VOCAB_FILE], a.texts[VOCAB_FILE], VOCAB_FILE)
    changed = [l for l in diff.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    assert len(changed) == 2 and changed[1] == '+guidance = "Evening mains."'


def test_create_without_vocab_entry_warns(texts):
    a = apply_proposal(texts, P(create_in_mealie=[{"kind": "tags", "name": "Duck"}]))
    assert not a.errors and "no entry in the vocabulary" in a.warnings[0]
    b = apply_proposal(texts, P(create_in_mealie=[{"kind": "tags", "name": "Duck"}],
                                vocab_ops=[{"op": "set", "kind": "tags", "name": "Duck",
                                            "roles": ["protein"]}]))
    assert not b.warnings


def test_propose_retries_on_warnings_too(texts):
    only_create = {"reply": "done", "create_in_mealie": [{"kind": "tags", "name": "Duck"}]}
    both = {"reply": "done", "create_in_mealie": [{"kind": "tags", "name": "Duck"}],
            "vocab_ops": [{"op": "set", "kind": "tags", "name": "Duck", "roles": ["protein"]}]}
    p, a = propose(ScriptedLLM(only_create, both), texts, {"tags": []},
                   [{"role": "user", "content": "add duck"}])
    assert p.vocab_ops and not a.warnings

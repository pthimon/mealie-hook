import json
import queue

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from mealie_hook.config import ROOT, Config
from mealie_hook.pipeline import REVIEW_NOTE_TITLE, Pipeline
from mealie_hook.rules import RulesStore
from mealie_hook.shopping import build, tick
from mealie_hook.state import State
from mealie_hook.web import MealieAuth, create_app

from fakes import FakeLLM, FakeMealie, base_recipe

ADMIN = {"username": "simon", "admin": True}


class StubWorker:
    def __init__(self):
        self.q, self.busy, self.last_results, self.triggers = queue.Queue(), False, [], []

    def trigger(self, reason, report_id=None):
        self.triggers.append((reason, report_id))


class FakeHA:
    enabled = True

    def __init__(self, items):
        self.data = items
        self.ticked = []

    def items(self):
        return self.data

    def tick(self, item_id):
        self.ticked.append(item_id)


def auth_as(user):
    def dep(request: Request):
        if user is None:
            raise HTTPException(401, "not logged in to Mealie")
        if not user.get("admin"):
            raise HTTPException(403, "Mealie admins only")
        return user
    return dep


CHAT_REPLY = {"reply": "Added Duck as a protein.",
              "vocab_ops": [{"op": "set", "kind": "tags", "name": "Duck", "roles": ["protein"]}]}


@pytest.fixture
def env(tmp_path):
    m = FakeMealie()
    llm = FakeLLM(EditProposal=CHAT_REPLY)
    cfg = Config(data_dir=tmp_path, process_since="2029-01-01T00:00:00Z")
    pipe = Pipeline(cfg, m, llm, State(tmp_path), RulesStore(tmp_path / "rules", ROOT / "rules"))
    worker = StubWorker()

    def client(user=ADMIN):
        return TestClient(create_app(pipe, worker, auth=auth_as(user)))
    return {"mealie": m, "pipe": pipe, "worker": worker, "client": client, "llm": llm}


def post(c, path, body=None, **headers):
    return c.post(path, content=json.dumps(body or {}),
                  headers={"Content-Type": "application/json", **headers})


def test_page_served_without_auth(env):
    r = env["client"](None).get("/ui/")
    assert r.status_code == 200 and "Recipe rules" in r.text


def test_api_needs_login_and_admin(env):
    assert env["client"](None).get("/ui/api/rules").status_code == 401
    assert env["client"]({"username": "x", "admin": False}).get("/ui/api/rules").status_code == 403


def test_writes_need_json_and_same_origin(env):
    c = env["client"]()
    assert c.post("/ui/api/chat", data={"messages": "x"}).status_code == 415
    r = post(c, "/ui/api/chat", {"messages": []}, Origin="https://evil.example")
    assert r.status_code == 403


def test_hook_is_internal_and_unauthenticated(env):
    body = {"event_type": "recipe_created", "document_data": json.dumps({"recipeSlug": "x"})}
    r = env["client"](None).post("/hook", content=json.dumps(body))
    assert r.status_code == 200 and env["worker"].triggers == [("event", None)]


def test_rules_view(env):
    r = env["client"]().get("/ui/api/rules").json()
    assert set(r["files"]) == {"vocabulary.toml", "ingredients.md", "classify.md", "labels.md"}
    assert "{{" not in r["rendered"]["classify.md"]
    assert any("has no guidance" in d["message"] or "not in Mealie" in d["message"]
               for d in r["drift"])


def test_chat_try_apply_revert(env):
    c = env["client"]()
    p = post(c, "/ui/api/chat", {"messages": [{"role": "user", "content": "duck is a protein"}]}).json()
    assert p["applicable"] and "Duck" in p["diffs"]["vocabulary.toml"]
    assert p["creates"] == [{"kind": "tags", "name": "Duck"}]
    assert p["history"].startswith("Added Duck")

    applied = post(c, f"/ui/api/proposals/{p['id']}/apply").json()
    assert "Duck" in applied["vocab"]["tags"]
    [h] = c.get("/ui/api/history").json()
    assert h["summary"] == "duck is a protein" and h["who"] == "simon"
    diff = c.get(f"/ui/api/history/{h['id']}").json()
    assert "+[tags.Duck]" in diff["vocabulary.toml"]

    post(c, f"/ui/api/history/{h['id']}/revert")
    assert "Duck" not in c.get("/ui/api/rules").json()["vocab"]["tags"]


def test_stale_proposal_refused(env):
    c = env["client"]()
    p = post(c, "/ui/api/chat", {"messages": [{"role": "user", "content": "x"}]}).json()
    rules = c.get("/ui/api/rules").json()
    c.put("/ui/api/files/labels.md", content=json.dumps({"content": rules["files"]["labels.md"] + "\n"}),
          headers={"Content-Type": "application/json"})
    assert post(c, f"/ui/api/proposals/{p['id']}/apply").status_code == 409


def test_manual_edit_is_validated(env):
    c = env["client"]()
    r = c.put("/ui/api/files/classify.md", content=json.dumps({"content": "no placeholders"}),
              headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and "placeholder" in r.json()["detail"]


def test_create_organizer(env):
    c = env["client"]()
    assert post(c, "/ui/api/organizers", {"kind": "tags", "name": "Duck"}).json()["existed"] is False
    assert env["mealie"].created_organizers == [("tags", "Duck")]
    assert post(c, "/ui/api/organizers", {"kind": "tags", "name": "Duck"}).json()["existed"] is True


def test_try_replays_with_proposed_rules(env):
    c = env["client"]()
    env["llm"].script.update(
        IngredientReply={"rows": [
            {"i": 0, "kind": "ingredient", "quantity": 2, "unit": None, "food": "carrot", "note": ""},
            {"i": 1, "kind": "heading", "quantity": 0, "unit": None, "food": "", "note": ""},
            {"i": 2, "kind": "ingredient", "quantity": 1, "unit": None, "food": "parsnip", "note": ""}]},
        ClassificationReply={"dish": "stew", "category": "Dinner", "tags": ["Winter"], "tools": []},
        LabelReply={"foods": [{"food": "parsnip", "label": "Fruit & Veg", "countable": True,
                               "plural": "parsnips"}]})
    p = post(c, "/ui/api/chat", {"messages": [{"role": "user", "content": "x"}]}).json()
    r = post(c, f"/ui/api/proposals/{p['id']}/try", {"slug": "test-stew"}).json()
    assert r["proposed"]["category"] == "Dinner" and r["proposed"]["tags"] == ["Winter"]
    assert env["mealie"].puts == []                        # a try never writes


def test_review_queue_and_clear(env):
    m = env["mealie"]
    r = base_recipe()
    r["tags"] = [{"id": "t-review", "name": "Needs review"}]
    r["notes"] = [{"title": REVIEW_NOTE_TITLE, "text": "- step 2 ends mid-sentence"},
                  {"title": "Mine", "text": "keep me"}]
    m._tags.append({"id": "t-review", "name": "Needs review"})
    m.recipes_by_slug["test-stew"] = r
    c = env["client"]()
    [item] = c.get("/ui/api/review").json()
    assert item["reasons"] == ["step 2 ends mid-sentence"] and item["url"] == "/g/home/r/test-stew"
    post(c, "/ui/api/review/test-stew/clear")
    after = m.recipes_by_slug["test-stew"]
    assert after["tags"] == [] and after["notes"] == [{"title": "Mine", "text": "keep me"}]


# ---------------------------------------------------------------------------- shopping

def test_shopping_build_merges_and_dedupes():
    m = FakeMealie()
    ha = FakeHA([{"id": "h1", "name": "Carrot", "complete": False},
                 {"id": "h2", "name": "Milk", "complete": False},
                 {"id": "h3", "name": "Bread", "complete": True}])
    s = build(m, ha)
    assert sorted(r["line"] for r in s["mealie"]) == ["bin bags", "carrots", "passata"]
    assert [(r["line"], r["duplicate"]) for r in s["ha"]] == [("Carrot", True), ("Milk", False)]
    assert sorted(s["text"].split("\n")) == ["Milk", "bin bags", "carrots", "passata"]


def test_shopping_quantities():
    s = build(FakeMealie(), None, quantities=True)
    assert "passata 400 g" in s["text"] and "carrots 2" in s["text"]


def test_tick_only_the_items_shown():
    m = FakeMealie()
    ha = FakeHA([])
    res = tick(m, ha, "L1", ["m1", "m2"], ["h1"])
    assert res == {"mealie": 2, "mealie_asked": 2, "ha": 1, "ha_failed": []}
    assert [i["id"] for i in m.shop_items if i["checked"]] == ["m1", "m2", "m4"]
    assert ha.ticked == ["h1"]


def test_ha_failure_does_not_block_mealie():
    class DeadHA(FakeHA):
        def tick(self, item_id):
            raise ConnectionError("down")
    m = FakeMealie()
    res = tick(m, DeadHA([]), "L1", ["m1"], ["h1"])
    assert res["mealie"] == 1 and res["ha"] == 0 and res["ha_failed"]


def test_shopping_endpoints(env):
    c = env["client"]()
    s = c.get("/ui/api/shopping").json()
    assert s["list"]["name"] == "Weekly shop" and not s["ha_enabled"]
    r = post(c, "/ui/api/shopping/tick", {"list_id": "L1", "mealie_ids": ["m3"]}).json()
    assert r["mealie"] == 1


def test_real_auth_rejects_missing_cookie():
    dep = MealieAuth("http://mealie.invalid/api")
    scope = {"type": "http", "headers": [], "method": "GET", "path": "/"}
    with pytest.raises(HTTPException) as e:
        dep(Request(scope))
    assert e.value.status_code == 401


SINCE = "2026-09-05T00:00:00+01:00"      # the forgotten shop, as the browser sends it


def test_since_splits_out_items_from_before_the_forgotten_shop():
    s = build(FakeMealie(), None, since=SINCE)
    old = {r["line"] for r in s["mealie"] if r["old"]}
    assert old == {"carrots"}                         # passata was topped up after, so new
    assert sorted(s["text"].split("\n")) == ["bin bags", "passata"]
    from mealie_hook.state import parse_ts
    assert parse_ts(s["since"]) == parse_ts("2026-09-04T23:00:00Z")   # same instant


def test_bought_item_does_not_hide_a_new_ha_request():
    ha = FakeHA([{"id": "h1", "name": "Carrot", "complete": False}])
    s = build(FakeMealie(), ha, since=SINCE)
    assert s["ha"] == [{"id": "h1", "line": "Carrot", "duplicate": False}]
    assert "Carrot" in s["text"].split("\n")


def test_no_since_means_nothing_is_old():
    assert not any(r["old"] for r in build(FakeMealie(), None)["mealie"])


def test_tick_records_last_tick_for_the_hint(env):
    c = env["client"]()
    assert c.get("/ui/api/shopping").json()["last_tick"] is None
    post(c, "/ui/api/shopping/tick", {"list_id": "L1", "mealie_ids": ["m1"]})
    assert c.get("/ui/api/shopping").json()["last_tick"]
    r = c.get("/ui/api/shopping", params={"since": SINCE}).json()
    assert [x["line"] for x in r["mealie"] if x["old"]] == []   # m1 is ticked now

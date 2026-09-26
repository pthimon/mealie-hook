"""HTTP: the internal webhook, and the rules / review / shopping page for people.

Two audiences on one app:

* `/hook`, `/sweep`, `/health` -- internal. Mealie's notifier POSTs to /hook over the
  container network. Caddy never routes here.
* `/ui/...` -- the page and its JSON API. Caddy serves it as recipes.<domain>/rules/ by
  rewriting that prefix to /ui, so it is same-origin with Mealie and the browser sends
  Mealie's own login cookie. Every /ui/api call checks that cookie against Mealie
  (`/api/users/self`) and requires an admin.

State-changing calls must be JSON (a cross-site form cannot send that without a CORS
preflight, which is never granted) and any Origin header must match the host: together with
the cookie's SameSite default, that closes CSRF.
"""

import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import chat, review, shopping
from .pipeline import Pipeline
from .rules import FILES, Rules, RulesError, unified

log = logging.getLogger(__name__)

UI_FILE = Path(__file__).with_name("ui") / "index.html"
COOKIE = "mealie.access_token"
AUTH_TTL = 300


class MealieAuth:
    """Validates a Mealie login token by asking Mealie who it belongs to. Cached briefly."""

    def __init__(self, mealie_url: str):
        self.url = mealie_url.rstrip("/")
        self.cache: dict[str, tuple[float, dict]] = {}

    def __call__(self, request: Request) -> dict:
        token = request.cookies.get(COOKIE, "")
        auth = request.headers.get("authorization", "")
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:]
        if not token:
            raise HTTPException(401, "not logged in to Mealie")
        hit = self.cache.get(token)
        if hit and hit[0] > time.monotonic():
            user = hit[1]
        else:
            try:
                r = httpx.get(f"{self.url}/users/self", timeout=10,
                              headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError:
                raise HTTPException(503, "cannot reach Mealie to check the login") from None
            if r.status_code != 200:
                raise HTTPException(401, "Mealie login expired")
            user = r.json()
            self.cache[token] = (time.monotonic() + AUTH_TTL, user)
        if not user.get("admin"):
            raise HTTPException(403, "Mealie admins only")
        return user


def csrf_guard(request: Request):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "JSON only")
    origin = request.headers.get("origin")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if origin and urlparse(origin).netloc != host:
        raise HTTPException(403, "cross-origin request refused")


# ------------------------------------------------------------------- request bodies

class ChatIn(BaseModel):
    messages: list[dict]


class TryIn(BaseModel):
    slug: str


class OrganizerIn(BaseModel):
    kind: str
    name: str


class FileIn(BaseModel):
    content: str


class TickIn(BaseModel):
    list_id: str
    mealie_ids: list[str] = []
    ha_ids: list[str] = []


def create_app(pipeline: Pipeline, worker, auth=None) -> FastAPI:
    from .server import handle_event                 # avoid an import cycle

    app = FastAPI(title="mealie-hook", docs_url=None, redoc_url=None, openapi_url=None)
    auth = auth or MealieAuth(pipeline.cfg.mealie_url)
    store = pipeline.rules
    proposals = chat.ProposalStore()
    cfg = pipeline.cfg
    ha = shopping.HomeAssistant(cfg.ha_url, cfg.ha_token) if cfg.ha_url else None
    user_dep = [Depends(csrf_guard)]

    def who(user: dict) -> str:
        return user.get("username") or user.get("email") or "?"

    # ------------------------------------------------------------------ internal

    @app.post("/hook")
    async def hook(request: Request):
        log.info("hook: %s", handle_event(worker, await request.body()))
        return {"ok": True}

    @app.post("/sweep", status_code=202)
    def sweep():
        worker.trigger("manual")
        return {"ok": True}

    @app.get("/health")
    def health():
        st = pipeline.state.data
        return {"ok": True, "busy": worker.busy, "queued": worker.q.qsize(),
                "last_sweep": st.get("last_sweep"), "since": st.get("since"),
                "last_results": worker.last_results}

    # ------------------------------------------------------------------------ page

    @app.get("/ui")
    def ui_redirect():
        return RedirectResponse("ui/")

    @app.get("/ui/")
    def ui_page():
        return FileResponse(UI_FILE, headers={"Cache-Control": "no-store"})

    @app.get("/ui/api/me")
    def me(user=Depends(auth)):
        return {"username": who(user), "ha": bool(ha and ha.enabled)}

    # ----------------------------------------------------------------------- rules

    def rules_view() -> dict:
        texts = store.texts()
        rules = Rules.from_texts(texts)
        live = pipeline.live_names()
        banned = pipeline.banned_tags(rules)
        return {
            "version": store.version(), "files": texts, "live": live,
            "vocab": rules.vocab.model_dump(), "drift": rules.drift(live),
            "rendered": {"classify.md": rules.render("classify.md", live, banned),
                         "labels.md": rules.render("labels.md", live)},
        }

    @app.get("/ui/api/rules")
    def get_rules(user=Depends(auth)):
        return rules_view()

    @app.put("/ui/api/files/{name}", dependencies=user_dep)
    def put_file(name: str, body: FileIn, user=Depends(auth)):
        if name not in FILES:
            raise HTTPException(404, "no such rules file")
        try:
            snap = store.save({name: body.content}, f"edited {name} by hand", who(user))
        except RulesError as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True, "snapshot": snap, **rules_view()}

    @app.post("/ui/api/organizers", dependencies=user_dep)
    def create_organizer(body: OrganizerIn, user=Depends(auth)):
        if body.kind not in ("categories", "tags", "tools", "labels"):
            raise HTTPException(422, "unknown kind")
        name = body.name.strip()
        if name in pipeline.live_names()[body.kind]:
            return {"ok": True, "existed": True}
        pipeline.mealie.create_organizer(body.kind, name)
        log.info("%s created %s %r in Mealie", who(user), body.kind, name)
        return {"ok": True, "existed": False}

    # ------------------------------------------------------------------------ chat

    @app.post("/ui/api/chat", dependencies=user_dep)
    def chat_turn(body: ChatIn, user=Depends(auth)):
        texts = store.texts()
        live = pipeline.live_names()
        try:
            proposal, applied = chat.propose(pipeline.llm, texts, live, body.messages)
        except Exception as e:
            log.exception("rules chat failed")
            raise HTTPException(502, f"the model did not answer: {e}") from None
        request_text = next((m.get("content", "") for m in reversed(body.messages)
                             if m.get("role") == "user"), "")
        p = proposals.add(base_version=store.version(), request=request_text,
                          proposal=proposal, applied=applied,
                          creates=chat.suggested_creates(proposal, live))
        return {**p.view(texts), "history": chat.history_line(p)}

    def _proposal(pid: str) -> chat.Proposal:
        p = proposals.get(pid)
        if not p:
            raise HTTPException(404, "proposal expired; ask again")
        return p

    @app.post("/ui/api/proposals/{pid}/try", dependencies=user_dep)
    def try_proposal(pid: str, body: TryIn, user=Depends(auth)):
        p = _proposal(pid)
        if p.applied.errors:
            raise HTTPException(422, "this proposal has errors")
        rules = Rules.from_texts(p.applied.texts)
        current = pipeline.mealie.recipe(body.slug)
        res = pipeline.replay(body.slug, rules)
        return {"proposed": res.model_dump(),
                "in_mealie": {"category": [c["name"] for c in current.get("recipeCategory") or []],
                              "tags": [t["name"] for t in current.get("tags") or []],
                              "tools": [t["name"] for t in current.get("tools") or []]}}

    @app.post("/ui/api/proposals/{pid}/apply", dependencies=user_dep)
    def apply_proposal(pid: str, user=Depends(auth)):
        p = _proposal(pid)
        if p.applied.errors or not p.applied.changed:
            raise HTTPException(422, "nothing valid to apply")
        if store.version() != p.base_version:
            raise HTTPException(409, "the rules changed since this was proposed; ask again")
        summary = (p.request.strip() or p.summary())[:200]
        snap = store.save({f: p.applied.texts[f] for f in p.applied.changed}, summary, who(user))
        log.info("%s applied rules change %s: %s", who(user), snap, summary)
        return {"ok": True, "snapshot": snap, **rules_view()}

    # --------------------------------------------------------------------- history

    @app.get("/ui/api/history")
    def history(user=Depends(auth)):
        return store.list_history()

    @app.get("/ui/api/history/{snap}")
    def history_diff(snap: str, user=Depends(auth)):
        """What the change recorded by `snap` did: its before-snapshot against the state
        right after it (the next snapshot, or the live files if it is the latest)."""
        entries = [h["id"] for h in store.list_history()]        # newest first
        if snap not in entries:
            raise HTTPException(404, "no such version")
        before = store.snapshot_texts(snap)
        i = entries.index(snap)
        after = store.snapshot_texts(entries[i - 1]) if i > 0 else store.texts()
        return {f: unified(before.get(f, ""), after.get(f, ""), f) for f in FILES
                if before.get(f, "") != after.get(f, "")}

    @app.post("/ui/api/history/{snap}/revert", dependencies=user_dep)
    def revert(snap: str, user=Depends(auth)):
        try:
            store.revert(snap, who(user))
        except RulesError as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True, **rules_view()}

    # ---------------------------------------------------------------------- review

    @app.get("/ui/api/review")
    def review_queue(user=Depends(auth)):
        return review.flagged(pipeline.mealie, cfg.review_tag)

    @app.post("/ui/api/review/{slug}/clear", dependencies=user_dep)
    def review_clear(slug: str, user=Depends(auth)):
        review.clear(pipeline.mealie, slug, cfg.review_tag)
        return {"ok": True}

    @app.get("/ui/api/recipes")
    def recipes(user=Depends(auth)):
        items = sorted(pipeline.mealie.recipes(), key=lambda r: r.get("createdAt") or "",
                       reverse=True)
        return [{"slug": r["slug"], "name": r["name"]} for r in items]

    # -------------------------------------------------------------------- shopping

    @app.get("/ui/api/shopping")
    def shopping_view(list_id: str | None = None, quantities: bool = False,
                      user=Depends(auth)):
        return shopping.build(pipeline.mealie, ha, list_id, quantities)

    @app.post("/ui/api/shopping/tick", dependencies=user_dep)
    def shopping_tick(body: TickIn, user=Depends(auth)):
        res = shopping.tick(pipeline.mealie, ha, body.list_id, body.mealie_ids, body.ha_ids)
        log.info("%s ticked off shopping: %s", who(user), res)
        return res

    @app.exception_handler(RulesError)
    def rules_error(request: Request, exc: RulesError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    return app


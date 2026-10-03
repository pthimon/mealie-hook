"""HTTP: the internal webhook, and the Mealie Toolkit page (plan, shopping, review, rules).

Two audiences on one app:

* `/hook`, `/sweep`, `/health` -- internal. Mealie's notifier POSTs to /hook over the
  container network. Caddy never routes here.
* `/ui/...` -- the page and its JSON API. Caddy serves it as recipes.<domain>/toolkit/ by
  rewriting that prefix to /ui, so it is same-origin with Mealie and the browser sends
  Mealie's own login cookie. Every /ui/api call checks that cookie against Mealie
  (`/api/users/self`). Any Mealie user gets the everyday tabs (Plan, Upcoming, Shopping,
  Discover); the curation ones (Needs review, Rules, Change rules, History) need Mealie's own
  "can organise" permission, which admins have.

State-changing calls must be JSON (a cross-site form cannot send that without a CORS
preflight, which is never granted) and any Origin header must match the host: together with
the cookie's SameSite default, that closes CSRF.
"""

import logging
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import bought, chat, discover, planner, review, shopping, upcoming
from .mealie import MealieError
from .pipeline import Pipeline
from .rules import FILES, Rules, RulesError, unified

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).with_name("ui")
UI_FILE = UI_DIR / "index.html"
ICONS = {p.name for p in (UI_DIR / "icons").glob("*") if p.suffix in (".png", ".svg")}
# Installable as its own app ("Add to Home Screen"), separate from Mealie's: its own scope,
# and its own id. Paths are relative, so they follow whatever prefix Caddy serves the page
# under. No "id": a relative id resolves against the site root, which is Mealie's own app id
# ("/"); left out, it defaults to start_url, i.e. this page.
MANIFEST = {
    "name": "Mealie Toolkit", "short_name": "Toolkit", "start_url": "./",
    "scope": "./", "display": "standalone", "theme_color": "#e58325",
    "background_color": "#ffffff",
    "description": "Meal plan to shopping list, upcoming ingredients, recipe discovery",
    "icons": [{"src": f"icons/{k}-{n}.png", "sizes": f"{n}x{n}", "type": "image/png",
               "purpose": k} for k in ("any", "maskable") for n in (192, 512)],
}
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
        return user


def can_organize(user: dict) -> bool:
    """Mealie's permission to manage categories, tags and tools -- what the rules decide."""
    return bool(user.get("admin") or user.get("canOrganize"))


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


class DiscoverIn(BaseModel):
    urls: list[str]


class MoveIn(BaseModel):
    id: int
    date: str


class SwapIn(BaseModel):
    a: int
    b: int


class PlanItemIn(BaseModel):
    slug: str
    scale: float
    refs: list[str] = []


class PlanEntryIn(BaseModel):
    id: int
    date: str | None = None
    slug: str | None = None
    scale: float | None = None


class PlanAddIn(BaseModel):
    list_id: str
    items: list[PlanItemIn]
    entries: list[PlanEntryIn] = []


def create_app(pipeline: Pipeline, worker, auth=None, goodfood=None) -> FastAPI:
    from .server import handle_event                 # avoid an import cycle

    app = FastAPI(title="mealie-toolkit", docs_url=None, redoc_url=None, openapi_url=None)
    auth = auth or MealieAuth(pipeline.cfg.mealie_url)
    store = pipeline.rules
    proposals = chat.ProposalStore()
    cfg = pipeline.cfg
    ha = shopping.HomeAssistant(cfg.ha_url, cfg.ha_token) if cfg.ha_url else None
    user_dep = [Depends(csrf_guard)]

    def organizer(user=Depends(auth)) -> dict:
        if not can_organize(user):
            raise HTTPException(403, "needs Mealie's \"can organise\" permission")
        return user
    goodfood = goodfood or discover.GoodFood(cfg.data_dir)

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

    # The app-install files are fetched by the browser without cookies: no login.
    @app.get("/ui/manifest.webmanifest")
    def manifest():
        return JSONResponse(MANIFEST, media_type="application/manifest+json")

    @app.get("/ui/icons/{name}")
    def icon(name: str):
        if name not in ICONS:
            raise HTTPException(404, "no such icon")
        return FileResponse(UI_DIR / "icons" / name, headers={"Cache-Control": "max-age=86400"})

    @app.get("/ui/sw.js")
    def service_worker():
        return FileResponse(UI_DIR / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/ui/api/me")
    def me(user=Depends(auth)):
        return {"username": who(user), "ha": bool(ha and ha.enabled),
                "can_organize": can_organize(user)}

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
    def get_rules(user=Depends(organizer)):
        return rules_view()

    @app.put("/ui/api/files/{name}", dependencies=user_dep)
    def put_file(name: str, body: FileIn, user=Depends(organizer)):
        if name not in FILES:
            raise HTTPException(404, "no such rules file")
        try:
            snap = store.save({name: body.content}, f"edited {name} by hand", who(user))
        except RulesError as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True, "snapshot": snap, **rules_view()}

    @app.post("/ui/api/organizers", dependencies=user_dep)
    def create_organizer(body: OrganizerIn, user=Depends(organizer)):
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
    def chat_turn(body: ChatIn, user=Depends(organizer)):
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
    def try_proposal(pid: str, body: TryIn, user=Depends(organizer)):
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
    def apply_proposal(pid: str, user=Depends(organizer)):
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
    def history(user=Depends(organizer)):
        return store.list_history()

    @app.get("/ui/api/history/{snap}")
    def history_diff(snap: str, user=Depends(organizer)):
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
    def revert(snap: str, user=Depends(organizer)):
        try:
            store.revert(snap, who(user))
        except RulesError as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True, **rules_view()}

    # ---------------------------------------------------------------------- review

    @app.get("/ui/api/review")
    def review_queue(user=Depends(organizer)):
        return review.flagged(pipeline.mealie, cfg.review_tag)

    @app.post("/ui/api/review/{slug}/clear", dependencies=user_dep)
    def review_clear(slug: str, user=Depends(organizer)):
        review.clear(pipeline.mealie, slug, cfg.review_tag)
        return {"ok": True}

    @app.get("/ui/api/recipes")
    def recipes(user=Depends(organizer)):
        items = sorted(pipeline.mealie.recipes(), key=lambda r: r.get("createdAt") or "",
                       reverse=True)
        return [{"slug": r["slug"], "name": r["name"]} for r in items]

    # -------------------------------------------------------------------- shopping

    @app.get("/ui/api/shopping")
    def shopping_view(list_id: str | None = None, quantities: bool = False,
                      since: str | None = None, user=Depends(auth)):
        view = shopping.build(pipeline.mealie, ha, list_id, quantities, since)
        try:
            bought.harvest(pipeline.mealie, pipeline.state)
        except Exception:
            log.exception("could not record ticked items as bought")
        return {**view, "last_tick": pipeline.state.data.get("last_tick")}

    @app.post("/ui/api/shopping/tick", dependencies=user_dep)
    def shopping_tick(body: TickIn, user=Depends(auth)):
        res = shopping.tick(pipeline.mealie, ha, body.list_id, body.mealie_ids, body.ha_ids,
                            pipeline.state)
        pipeline.state.ticked()
        log.info("%s ticked off shopping: %s", who(user), res)
        return res

    # ---------------------------------------------------------------- plan -> list

    @app.get("/ui/api/plan")
    def plan_view(start: str | None = None, end: str | None = None, user=Depends(auth)):
        d_start, d_end = planner.default_range()
        lists = pipeline.mealie.shopping_lists()
        view = planner.build(pipeline.mealie, pipeline.state, start or d_start, end or d_end,
                             cfg.plan_default_servings, lists)
        return {**view, "lists": [{"id": x["id"], "name": x["name"]} for x in lists]}

    @app.post("/ui/api/plan/add", dependencies=user_dep)
    def plan_add(body: PlanAddIn, user=Depends(auth)):
        try:
            res = planner.add(pipeline.mealie, pipeline.state, body.list_id,
                              [i.model_dump() for i in body.items],
                              [e.model_dump() for e in body.entries])
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        log.info("%s added the plan to shopping list %s: %s", who(user), body.list_id, res)
        return res

    # -------------------------------------------------------------------- upcoming

    @app.get("/ui/api/upcoming")
    def upcoming_view(today: str | None = None, since: str | None = None, user=Depends(auth)):
        try:
            day = date.fromisoformat(today) if today else date.today()
            start = date.fromisoformat(since) if since else None
        except ValueError:
            raise HTTPException(422, "dates must be YYYY-MM-DD") from None
        return upcoming.build(pipeline.mealie, pipeline.state, day, cfg.plan_default_servings,
                              since=start)

    @app.post("/ui/api/upcoming/move", dependencies=user_dep)
    def upcoming_move(body: MoveIn, user=Depends(auth)):
        try:
            day = date.fromisoformat(body.date)
        except ValueError:
            raise HTTPException(422, "date must be YYYY-MM-DD") from None
        res = upcoming.move(pipeline.mealie, body.id, day)
        log.info("%s moved plan entry %s: %s -> %s", who(user), body.id, res["from"], res["to"])
        return res

    @app.post("/ui/api/upcoming/swap", dependencies=user_dep)
    def upcoming_swap(body: SwapIn, user=Depends(auth)):
        res = upcoming.swap(pipeline.mealie, body.a, body.b)
        log.info("%s swapped plan entries %s and %s", who(user), body.a, body.b)
        return res

    # -------------------------------------------------------------------- discover

    @app.get("/ui/api/discover")
    def discover_search(request: Request, page: int = 1, user=Depends(auth)):
        qp = request.query_params
        params = {"q": qp.get("q"), "sort": qp.get("sort"),
                  **{n: qp.getlist(n) for n in discover.FILTERS}}
        try:
            return discover.search(pipeline.mealie, goodfood, params, page)
        except discover.DiscoverError as e:
            raise HTTPException(502, str(e)) from None

    @app.post("/ui/api/discover/import", dependencies=user_dep)
    def discover_import(body: DiscoverIn, user=Depends(auth)):
        res = discover.import_urls(pipeline.mealie, body.urls[:100])
        log.info("%s queued %d Good Food recipes for import (report %s)", who(user),
                 res["queued"], res["report"])
        return res

    @app.exception_handler(MealieError)
    def mealie_error(request: Request, exc: MealieError):
        log.warning("mealie: %s", exc)
        return JSONResponse({"detail": f"Mealie refused: {exc}"}, status_code=502)

    @app.exception_handler(RulesError)
    def rules_error(request: Request, exc: RulesError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    return app


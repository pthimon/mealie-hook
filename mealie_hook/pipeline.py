"""The per-recipe pipeline and the sweep that feeds it.

Order matters: repair the scrape before parsing (a truncated ingredient list parses
"successfully" into a truncated recipe), parse before classifying (the classifier reads
food names), and label foods before they are created so they never exist unlabelled.
"""

import copy
import logging
import time

from . import MARKER_KEY, __version__, classify, ingredients, labels, scrape
from .config import Config
from .foods import Vocab, norm
from .ingredients import NEW
from .llm import LLM, LLMError, load_prompt
from .mealie import Mealie, MealieError
from .models import Result
from .state import State, now_iso, parse_ts

log = logging.getLogger(__name__)

REVIEW_NOTE_TITLE = "Needs review (mealie-hook)"


def _merge(current: list[dict] | None, add: list[dict]) -> list[dict]:
    """Add organizers by name; never remove one a human or the scraper set."""
    out = list(current or [])
    have = {norm(x.get("name")) for x in out}
    for x in add:
        if norm(x.get("name")) not in have:
            out.append(x)
            have.add(norm(x.get("name")))
    return out


def _stamp(recipe: dict) -> str | None:
    return recipe.get("dateUpdated") or recipe.get("updatedAt")


def preview_rows(ings: list[dict]) -> list[str]:
    out = []
    for ing in ings:
        f, u = ing.get("food"), ing.get("unit")
        title = f"[{ing['title']}] " if ing.get("title") else ""
        if not isinstance(f, dict):
            out.append(f"{title}(as written) {ingredients.raw_text(ing)}")
            continue
        q = ing.get("quantity") or 0
        qty = f"{q:g}" if q else "-"
        unit = u["name"] if isinstance(u, dict) else ""
        new = " (NEW)" if f.get(NEW) else ""
        note = f"  // {ing['note']}" if ing.get("note") else ""
        out.append(f"{title}{qty} {unit} | {f['name']}{new}{note}".replace("  |", " |"))
    return out


class Pipeline:
    def __init__(self, cfg: Config, mealie: Mealie, llm: LLM, state: State):
        self.cfg, self.mealie, self.llm, self.state = cfg, mealie, llm, state

    def prompt(self, name: str) -> str:
        # Read on every use, so a prompt edit on disk takes effect without a restart.
        return load_prompt(self.cfg.prompts_dir, name)

    # ------------------------------------------------------------------ candidates

    def candidates(self) -> list[str]:
        """Scraped recipes created since the cut-off and not yet processed."""
        since = self.state.since(self.cfg.process_since)
        out = []
        for r in self.mealie.recipes():
            if not r.get("orgURL") or self.state.is_done(r["slug"]):
                continue
            created = parse_ts(r.get("createdAt") or r.get("dateAdded"))
            if created and created >= since:
                out.append(r["slug"])
        return out

    def sweep(self, dry: bool = False) -> list[Result]:
        results = []
        for slug in self.candidates():
            try:
                res = self.process(slug, dry=dry)
            except Exception as e:  # one bad recipe must not stop the rest
                log.exception("processing %s failed", slug)
                if not dry:
                    n = self.state.bump_attempt(slug)
                    if n >= self.cfg.max_attempts:
                        self._give_up(slug, e, n)
                results.append(Result(slug=slug, status="failed", reason=f"{type(e).__name__}: {e}"))
                continue
            if not dry and res.status in ("written", "skipped"):
                self.state.mark_done(slug)
            results.append(res)
        if not dry:
            self.state.swept()
        return results

    def _give_up(self, slug: str, err: Exception, attempts: int):
        """Stop retrying: mark it processed and flag it, so a human sees it."""
        r = self.mealie.recipe(slug)
        r["extras"] = {**(r.get("extras") or {}), MARKER_KEY: f"{__version__} failed {now_iso()}"}
        r["notes"] = [n for n in r.get("notes") or [] if n.get("title") != REVIEW_NOTE_TITLE]
        r["notes"].append({"title": REVIEW_NOTE_TITLE,
                           "text": f"- automatic processing failed {attempts} times: {err}"})
        r["tags"] = _merge(r.get("tags"), [self._review_tag()])
        self.mealie.put_recipe(slug, r)
        self.state.mark_done(slug)
        log.warning("gave up on %s after %d attempts", slug, attempts)

    def _review_tag(self) -> dict:
        for t in self.mealie.tags():
            if norm(t["name"]) == norm(self.cfg.review_tag):
                return t
        return self.mealie.create_tag(self.cfg.review_tag)

    # --------------------------------------------------------------------- process

    def process(self, slug: str, dry: bool = False, force: bool = False) -> Result:
        t0 = time.monotonic()
        res = Result(slug=slug)
        r = self.mealie.recipe(slug)
        if not force:
            if not r.get("orgURL"):
                res.status, res.reason = "skipped", "not imported from a URL"
                return res
            if MARKER_KEY in (r.get("extras") or {}):
                res.status, res.reason = "skipped", "already processed"
                return res
        before = copy.deepcopy(r)
        stamp = _stamp(r)
        flags, changes = res.flags, res.changes

        self._fix_scrape(r, flags, changes)
        foods_list = self.mealie.foods()
        vocab = Vocab(foods_list, self.mealie.units())
        equipment = self._parse_ingredients(r, vocab, flags, changes)
        new_foods, new_units = ingredients.new_records(r.get("recipeIngredient") or [])
        res.new_foods, res.new_units = new_foods, new_units
        tag_recs = self._classify(r, equipment, res)
        label_plan, label_recs = self._plan_labels(new_foods, foods_list, flags)

        r["extras"] = {**(r.get("extras") or {}), MARKER_KEY: f"{__version__} {now_iso()}"}
        notes = [n for n in r.get("notes") or [] if n.get("title") != REVIEW_NOTE_TITLE]
        if flags:
            notes.append({"title": REVIEW_NOTE_TITLE,
                          "text": "\n".join(f"- {f}" for f in flags)})
        r["notes"] = notes
        res.preview = preview_rows(r.get("recipeIngredient") or [])

        if dry or self.cfg.dry_run:
            res.status = "dry-run"
            res.seconds = round(time.monotonic() - t0, 1)
            self.state.save_run(slug, res.model_dump())
            return res

        # Someone may have opened the recipe and edited it while the model was working.
        # Writing now would silently throw their edit away, so back off and retry later.
        if _stamp(self.mealie.recipe(slug)) != stamp:
            res.status, res.reason = "deferred", "recipe was edited during processing; will retry"
            return res

        if flags:
            review = tag_recs.get(self.cfg.review_tag) or self._review_tag()
            r["tags"] = _merge(r.get("tags"), [review])
        self._create_records(r, new_foods, new_units, label_plan, label_recs)
        self.state.snapshot(slug, before)
        self.mealie.put_recipe(slug, r)
        res.status = "written"
        res.seconds = round(time.monotonic() - t0, 1)
        self.state.save_run(slug, res.model_dump())
        log.info("%s: written in %.0fs, %d flag(s)", slug, res.seconds, len(flags))
        return res

    # ---------------------------------------------------------------------- stages

    def _fix_scrape(self, r: dict, flags: list[str], changes: list[str]):
        problems, _ = scrape.scrape_problems(r)
        page = None
        if problems or scrape.missing_nutrition(r) or not r.get("recipeServings"):
            try:
                page = scrape.extract_from_page(r["orgURL"], scrape.fetch_html(r["orgURL"]))
            except Exception as e:
                log.warning("%s: could not fetch source page: %s", r["slug"], e)
                if problems:
                    flags.append(f"could not fetch the source page to repair the import: {e}")
        if problems and page:
            changes += scrape.repair(r, page)
            problems, _ = scrape.scrape_problems(r)
        flags += [f"import looks incomplete: {p}" for p in problems]
        if page:
            added = scrape.fill_nutrition(r, page.get("nutrition") or {})
            if added:
                changes.append("nutrition filled from the source page: " + ", ".join(added))
            if not r.get("recipeServings") and page.get("servings"):
                r["recipeServings"] = page["servings"]
                changes.append(f"servings set to {page['servings']} from the source page")
        settings = dict(r.get("settings") or {})
        if not settings.get("showNutrition"):
            settings["showNutrition"] = True
            r["settings"] = settings
            changes.append("nutrition panel shown")

    def _parse_ingredients(self, r: dict, vocab: Vocab, flags: list[str],
                           changes: list[str]) -> list[str]:
        ings = r.get("recipeIngredient") or []
        lines = ingredients.unparsed_lines(ings)
        if not lines:
            return []
        try:
            rows = ingredients.call_model(self.llm, self.prompt("ingredients"), lines, vocab)
        except (LLMError, ValueError) as e:
            flags.append(f"ingredient parse failed, rows left as written: {e}")
            return []
        pr = ingredients.apply_rows(ings, lines, rows, vocab)
        r["recipeIngredient"] = pr.ingredients
        flags += pr.flags
        headings = sum(1 for row in rows if row.kind == "heading")
        changes.append(f"ingredients: {pr.parsed} parsed, {pr.left_raw} left as written, "
                       f"{headings} heading(s) made section titles, "
                       f"{len(pr.equipment)} equipment row(s) removed")
        if pr.equipment:
            changes.append("equipment removed: " + "; ".join(pr.equipment))
        return pr.equipment

    def _classify(self, r: dict, equipment: list[str], res: Result) -> dict[str, dict]:
        cats = {c["name"]: c for c in self.mealie.categories()}
        tags = {t["name"]: t for t in self.mealie.tags()}
        tools = {t["name"]: t for t in self.mealie.tools()}
        banned = set(self.cfg.provenance_tags) | {self.cfg.review_tag}
        try:
            c = classify.call_model(self.llm, self.prompt("classify"),
                                    classify.recipe_brief(r, equipment), list(cats),
                                    [t for t in tags if t not in banned], list(tools))
        except LLMError as e:
            res.flags.append(f"classification failed: {e}")
            return tags
        c = classify.apply_rules(c, r, set(tags), set(tools), banned)
        res.flags += c.flags
        if not any(x.get("name") in cats for x in r.get("recipeCategory") or []):
            if c.category in cats:
                r["recipeCategory"] = [cats[c.category]]
        r["tags"] = _merge(r.get("tags"), [tags[t] for t in c.tags])
        r["tools"] = _merge(r.get("tools"), [tools[t] for t in c.tools])
        res.category = (r.get("recipeCategory") or [{}])[0].get("name")
        res.tags = [t["name"] for t in r["tags"]]
        res.tools = [t["name"] for t in r["tools"]]
        return tags

    def _plan_labels(self, new_foods: list[str], foods_list: list[dict],
                     flags: list[str]) -> tuple[dict, dict]:
        if not new_foods:
            return {}, {}
        label_recs = {x["name"]: x for x in self.mealie.labels()}
        examples: dict[str, list[str]] = {}
        for f in foods_list:
            name = (f.get("label") or {}).get("name")
            if name:
                examples.setdefault(name, []).append(f["name"])
        try:
            plan = labels.call_model(self.llm, self.prompt("labels"), new_foods,
                                     list(label_recs), examples)
        except LLMError as e:
            flags.append(f"aisle labelling failed; new foods left unlabelled: {e}")
            return {}, label_recs
        missing = [f for f in new_foods if f not in plan]
        if missing:
            flags.append(f"no aisle label chosen for: {', '.join(missing)}")
        return plan, label_recs

    def _create_records(self, r: dict, new_foods: list[str], new_units: list[str],
                        plan: dict, label_recs: dict):
        units = {norm(n): self._ensure(self.mealie.units, self.mealie.create_unit, n)
                 for n in new_units}
        foods = {}
        for name in new_foods:
            p = plan.get(name) or {}
            label = label_recs.get(p.get("label") or "")
            foods[norm(name)] = self._ensure(
                self.mealie.foods,
                lambda n, p=p, label=label: self.mealie.create_food(
                    n, p.get("plural"), label["id"] if label else None),
                name)
        for ing in r.get("recipeIngredient") or []:
            f, u = ing.get("food"), ing.get("unit")
            if isinstance(f, dict) and f.get(NEW):
                ing["food"] = foods[norm(f["name"])]
            if isinstance(u, dict) and u.get(NEW):
                ing["unit"] = units[norm(u["name"])]

    @staticmethod
    def _ensure(list_fn, create_fn, name: str) -> dict:
        """Create a record, or fetch it if it appeared since the vocabulary was read."""
        try:
            return create_fn(name)
        except MealieError:
            for rec in list_fn():
                if norm(rec.get("name")) == norm(name):
                    return rec
            raise

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
from .foods import UNIT_PLURALS, Vocab, norm
from .ingredients import NEW
from .llm import LLM, LLMError
from .mealie import Mealie, MealieError
from .models import Result
from .rules import Rules, RulesStore
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


def as_fresh_import(recipe: dict) -> dict:
    """A copy of a filed recipe as a fresh scrape would have left it: raw ingredient lines
    (section titles back as heading rows) and no category, tags or tools. Used to try rule
    changes, and by `eval`, against recipes whose right answer is already known."""
    r = copy.deepcopy(recipe)
    raw = []
    for ing in recipe.get("recipeIngredient") or []:
        if ing.get("title"):
            raw.append({"note": ing["title"], "originalText": ing["title"], "food": None})
        text = ing.get("originalText") or ing.get("display") or ""
        raw.append({"note": text, "originalText": text, "food": None, "unit": None,
                    "quantity": 0})
    r.update(recipeIngredient=raw, recipeCategory=[], tags=[], tools=[])
    return r


class Pipeline:
    def __init__(self, cfg: Config, mealie: Mealie, llm: LLM, state: State,
                 rules: RulesStore):
        self.cfg, self.mealie, self.llm, self.state, self.rules = cfg, mealie, llm, state, rules

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

    def replay(self, slug: str, rules: Rules) -> Result:
        """Run the model stages on a filed recipe as if freshly imported, writing nothing."""
        t0 = time.monotonic()
        res = Result(slug=slug, status="replay")
        r = as_fresh_import(self.mealie.recipe(slug))
        foods_list = self.mealie.foods()
        vocab = self._vocab(foods_list, rules)
        equipment = self._parse_ingredients(r, vocab, rules, res.flags, res.changes)
        res.new_foods, res.new_units = ingredients.new_records(r.get("recipeIngredient") or [])
        self._classify(r, equipment, rules, res)
        plan, _ = self._plan_labels(res.new_foods, foods_list, rules, res.flags)
        res.changes += [f"new food {f!r} -> {p.get('label')}" + (f" (plural {p['plural']!r})"
                        if p.get("plural") else "") for f, p in plan.items()]
        res.preview = preview_rows(r.get("recipeIngredient") or [])
        res.seconds = round(time.monotonic() - t0, 1)
        return res

    def _vocab(self, foods_list: list[dict], rules: Rules) -> Vocab:
        return Vocab(foods_list, self.mealie.units(), rules.vocab.with_role("labels", "herbs"))

    def process(self, slug: str, dry: bool = False, force: bool = False,
                rules: Rules | None = None) -> Result:
        t0 = time.monotonic()
        # Read on every recipe, so an edit from the rules page applies to the next one.
        rules = rules or self.rules.load()
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
        vocab = self._vocab(foods_list, rules)
        equipment = self._parse_ingredients(r, vocab, rules, flags, changes)
        new_foods, new_units = ingredients.new_records(r.get("recipeIngredient") or [])
        res.new_foods, res.new_units = new_foods, new_units
        tag_recs = self._classify(r, equipment, rules, res)
        label_plan, label_recs = self._plan_labels(new_foods, foods_list, rules, flags)

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

    def _parse_ingredients(self, r: dict, vocab: Vocab, rules: Rules, flags: list[str],
                           changes: list[str]) -> list[str]:
        ings = r.get("recipeIngredient") or []
        lines = ingredients.unparsed_lines(ings)
        if not lines:
            return []
        try:
            rows = ingredients.call_model(self.llm, rules.render("ingredients.md", {}), lines,
                                          vocab)
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

    def banned_tags(self, rules: Rules) -> set[str]:
        return set(rules.vocab.with_role("tags", "provenance")) | {self.cfg.review_tag}

    def live_names(self) -> dict[str, list[str]]:
        return {"categories": [c["name"] for c in self.mealie.categories()],
                "tags": [t["name"] for t in self.mealie.tags()],
                "tools": [t["name"] for t in self.mealie.tools()],
                "labels": [x["name"] for x in self.mealie.labels()]}

    def _classify(self, r: dict, equipment: list[str], rules: Rules,
                  res: Result) -> dict[str, dict]:
        cats = {c["name"]: c for c in self.mealie.categories()}
        tags = {t["name"]: t for t in self.mealie.tags()}
        tools = {t["name"]: t for t in self.mealie.tools()}
        banned = self.banned_tags(rules)
        live = {"categories": list(cats), "tags": list(tags), "tools": list(tools)}
        try:
            c = classify.call_model(self.llm, rules.render("classify.md", live, banned),
                                    classify.recipe_brief(r, equipment), list(cats),
                                    [t for t in tags if t not in banned], list(tools))
        except LLMError as e:
            res.flags.append(f"classification failed: {e}")
            return tags
        c = classify.apply_rules(c, r, set(tags), set(tools), banned, rules.vocab)
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

    def _plan_labels(self, new_foods: list[str], foods_list: list[dict], rules: Rules,
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
            plan = labels.call_model(self.llm,
                                     rules.render("labels.md", {"labels": list(label_recs)}),
                                     new_foods, list(label_recs), examples)
        except LLMError as e:
            flags.append(f"aisle labelling failed; new foods left unlabelled: {e}")
            return {}, label_recs
        missing = [f for f in new_foods if f not in plan]
        if missing:
            flags.append(f"no aisle label chosen for: {', '.join(missing)}")
        return plan, label_recs

    def _create_records(self, r: dict, new_foods: list[str], new_units: list[str],
                        plan: dict, label_recs: dict):
        units = {norm(n): self._ensure(
                     self.mealie.units,
                     lambda n: self.mealie.create_unit(n, UNIT_PLURALS.get(n)), n)
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

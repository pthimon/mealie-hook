"""Replay already-processed recipes through the model and score it against what is there.

Every recipe in the collection was parsed and filed by hand (by Claude, under the old
skill). Rebuilding each one's raw lines -- section titles become heading rows again -- and
running them through this pipeline gives a direct agreement score, with no writes.
"""

import json
import logging
from pathlib import Path

from . import classify, ingredients
from .foods import Vocab, canonical_unit, norm
from .llm import LLMError
from .pipeline import Pipeline, as_fresh_import
from .rules import Rules
from .state import now_iso

log = logging.getLogger(__name__)


def _unit(u) -> str:
    return canonical_unit(u.get("name")) if isinstance(u, dict) else ""


def _expected(recipe: dict) -> list[dict]:
    out = []
    for ing in recipe.get("recipeIngredient") or []:
        f = ing.get("food") or {}
        out.append({"title": norm(ing.get("title")), "food": norm(f.get("name")),
                    "unit": _unit(ing.get("unit")), "quantity": ing.get("quantity") or 0,
                    "text": ing.get("originalText") or ing.get("display") or ""})
    return out


def evaluate(pipe: Pipeline, n: int = 20, do_classify: bool = True,
             slugs: list[str] | None = None, rules: Rules | None = None) -> dict:
    rules = rules or pipe.rules.load()
    mealie = pipe.mealie
    summaries = [r for r in mealie.recipes() if r.get("orgURL")]
    summaries.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    if slugs:
        summaries = [r for r in summaries if r["slug"] in slugs]
    vocab_foods, vocab_units = mealie.foods(), mealie.units()
    live = pipe.live_names()
    cats, tools = live["categories"], live["tools"]
    banned = pipe.banned_tags(rules)
    tags = [t for t in live["tags"] if t not in banned]
    ing_prompt = rules.render("ingredients.md", {})
    cls_prompt = rules.render("classify.md", live, banned)

    totals = {"rows": 0, "food": 0, "unit": 0, "quantity": 0, "title": 0, "all": 0,
              "recipes": 0, "category": 0, "tags_tp": 0, "tags_fp": 0, "tags_fn": 0}
    report = []
    for s in summaries:
        if totals["recipes"] >= n:
            break
        recipe = mealie.recipe(s["slug"])
        if any(i.get("food") is None for i in recipe.get("recipeIngredient") or []):
            continue                                  # not fully parsed; no ground truth
        totals["recipes"] += 1
        raw = as_fresh_import(recipe)["recipeIngredient"]
        expected = _expected(recipe)
        vocab = Vocab(vocab_foods, vocab_units, rules.vocab.with_role("labels", "herbs"))
        lines = ingredients.unparsed_lines(raw)
        entry = {"slug": s["slug"], "mismatches": []}
        try:
            rows = ingredients.call_model(pipe.llm, ing_prompt, lines, vocab)
            got = ingredients.apply_rows(raw, lines, rows, vocab).ingredients
        except (LLMError, ValueError) as e:
            entry["error"] = str(e)
            report.append(entry)
            continue
        if len(got) != len(expected):
            entry["mismatches"].append(f"row count {len(got)} vs {len(expected)}")
        for exp, g in zip(expected, got):
            f = g.get("food")
            actual = {"title": norm(g.get("title")),
                      "food": norm(f.get("name")) if isinstance(f, dict) else "(raw)",
                      "unit": _unit(g.get("unit")), "quantity": g.get("quantity") or 0}
            ok = {"food": actual["food"] == exp["food"], "unit": actual["unit"] == exp["unit"],
                  "quantity": abs(float(actual["quantity"]) - float(exp["quantity"])) < 0.01,
                  "title": actual["title"] == exp["title"]}
            totals["rows"] += 1
            for k, v in ok.items():
                totals[k] += v
            totals["all"] += all(ok.values())
            if not all(ok.values()):
                entry["mismatches"].append(
                    f"{exp['text']!r}: want {exp['quantity']:g} {exp['unit']} {exp['food']!r}"
                    f"{' [' + exp['title'] + ']' if exp['title'] else ''}"
                    f" / got {float(actual['quantity']):g} {actual['unit']} {actual['food']!r}"
                    f"{' [' + actual['title'] + ']' if actual['title'] else ''}")

        if do_classify:
            want_cat = [c["name"] for c in recipe.get("recipeCategory") or []]
            want_tags = {t["name"] for t in recipe.get("tags") or []} - banned
            try:
                c = classify.call_model(pipe.llm, cls_prompt, classify.recipe_brief(recipe, []),
                                        cats, tags, tools)
                c = classify.apply_rules(c, recipe, set(tags), set(tools), banned, rules.vocab)
            except LLMError as e:
                entry["classify_error"] = str(e)
            else:
                got_tags = set(c.tags)
                totals["category"] += c.category in want_cat
                totals["tags_tp"] += len(got_tags & want_tags)
                totals["tags_fp"] += len(got_tags - want_tags)
                totals["tags_fn"] += len(want_tags - got_tags)
                entry["classify"] = {"category": [want_cat, c.category],
                                     "tags_missing": sorted(want_tags - got_tags),
                                     "tags_extra": sorted(got_tags - want_tags),
                                     "dish": c.dish}
        report.append(entry)
        log.info("eval %s: %d mismatch(es)", s["slug"], len(entry["mismatches"]))

    out = {"at": now_iso(), "totals": totals, "recipes": report}
    path = Path(pipe.cfg.data_dir) / "eval" / f"{out['at'].replace(':', '')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    out["path"] = str(path)
    return out

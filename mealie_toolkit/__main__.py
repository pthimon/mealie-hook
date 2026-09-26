"""Command line: `python -m mealie_toolkit <command>`.

    serve                          run the webhook receiver and worker (the container default)
    candidates                     list recipes the next sweep would process
    process SLUG... [--dry] [--force]
                                   process named recipes now (--force ignores the marker
                                   and the not-scraped check)
    sweep [--dry]                  process every candidate now
    eval [N] [--slug S]... [--no-classify]
                                   replay N already-processed recipes and score agreement
    undo SLUG                      restore the recipe from its latest pre-edit snapshot
    notifier [--create | --delete] [--name N] [--url U]
                                   show, create/enable, or remove the Mealie notifier
                                   (default name mealie-toolkit, url json://mealie-toolkit:PORT/hook)
    export [DIR]                   write the tracking JSON files from Mealie (default ./export)
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config
from .llm import LLM
from .mealie import Mealie
from .pipeline import Pipeline
from .rules import RulesStore
from .state import State


def build(cfg: Config) -> Pipeline:
    if not cfg.mealie_token:
        sys.exit("no Mealie token: set MEALIE_TOKEN or MEALIE_TOKEN_FILE")
    return Pipeline(cfg, Mealie(cfg.mealie_url, cfg.mealie_token),
                    LLM(cfg.llm_url, cfg.llm_model, cfg.llm_timeout), State(cfg.data_dir),
                    RulesStore(cfg.rules_dir, cfg.default_rules_dir))


def show(res):
    print(f"\n== {res.slug}: {res.status} {res.reason}".rstrip())
    for c in res.changes:
        print(f"   change: {c}")
    if res.category or res.tags or res.tools:
        print(f"   category: {res.category}   tags: {res.tags}   tools: {res.tools}")
    if res.new_foods or res.new_units:
        print(f"   new foods: {res.new_foods}   new units: {res.new_units}")
    for p in res.preview:
        print(f"     {p}")
    for f in res.flags:
        print(f"   REVIEW: {f}")
    if res.seconds:
        print(f"   ({res.seconds:.0f}s)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mealie_toolkit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-classify", action="store_true")
    ap.add_argument("--slug", action="append", default=[])
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--name", default="mealie-toolkit")
    ap.add_argument("--url")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config()

    if a.command == "serve":
        from .server import serve
        serve(build(cfg), cfg.port, cfg.settle_seconds, cfg.sweep_minutes)
    elif a.command == "candidates":
        pipe = build(cfg)
        print(f"since {pipe.state.since(cfg.process_since).isoformat()}:")
        for slug in pipe.candidates():
            print(f"  {slug}")
    elif a.command == "process":
        if not a.args:
            sys.exit("process needs at least one slug")
        pipe = build(cfg)
        for slug in a.args:
            res = pipe.process(slug, dry=a.dry, force=a.force)
            if res.status in ("written", "skipped") and not a.dry:
                pipe.state.mark_done(slug)
            show(res)
    elif a.command == "sweep":
        for res in build(cfg).sweep(dry=a.dry):
            show(res)
    elif a.command == "eval":
        from .evaluate import evaluate
        n = int(a.args[0]) if a.args else 20
        out = evaluate(build(cfg), n, not a.no_classify, a.slug or None)
        for r in out["recipes"]:
            print(f"\n== {r['slug']}" + (f"  ERROR {r['error']}" if r.get("error") else ""))
            for m in r["mismatches"]:
                print(f"   {m}")
            if c := r.get("classify"):
                print(f"   category want {c['category'][0]} got {c['category'][1]!r};"
                      f" tags missing {c['tags_missing']} extra {c['tags_extra']}")
        t = out["totals"]
        rows = max(t["rows"], 1)
        print(f"\n{t['recipes']} recipes, {t['rows']} rows: all fields {t['all'] / rows:.0%}, "
              f"food {t['food'] / rows:.0%}, unit {t['unit'] / rows:.0%}, "
              f"quantity {t['quantity'] / rows:.0%}, title {t['title'] / rows:.0%}")
        if not a.no_classify and t["recipes"]:
            tp, fp, fn = t["tags_tp"], t["tags_fp"], t["tags_fn"]
            print(f"category {t['category'] / t['recipes']:.0%}; tags precision "
                  f"{tp / max(tp + fp, 1):.0%} recall {tp / max(tp + fn, 1):.0%}")
        print(f"full report: {out['path']}")
    elif a.command == "undo":
        if len(a.args) != 1:
            sys.exit("undo needs one slug")
        pipe = build(cfg)
        snap = pipe.state.latest_snapshot(a.args[0])
        if not snap:
            sys.exit(f"no snapshot for {a.args[0]}")
        recipe = json.loads(snap.read_text())
        if not a.dry:
            pipe.mealie.put_recipe(a.args[0], recipe)
        print(f"{'would restore' if a.dry else 'restored'} {a.args[0]} from {snap}"
              " (foods and units it created are left in place)")
    elif a.command == "notifier":
        mealie = Mealie(cfg.mealie_url, cfg.mealie_token)
        url = a.url or f"json://mealie-toolkit:{cfg.port}/hook"
        if a.create:
            n = mealie.ensure_notifier(a.name, url)
            print(f"notifier {n['name']!r} enabled={n['enabled']} "
                  f"recipeCreated={n['options']['recipeCreated']} -> {url}")
        elif a.delete:
            print("deleted" if mealie.delete_notifier(a.name) else f"no notifier {a.name!r}")
        else:
            found = [x for x in mealie.notifiers() if x["name"] == a.name]
            print(json.dumps(found, indent=1) if found else f"no notifier {a.name!r}; "
                  f"create it with: notifier --create  (Apprise URL {url})")
    elif a.command == "export":
        from .export import export
        out = Path(a.args[0]) if a.args else Path("export")
        for p in export(Mealie(cfg.mealie_url, cfg.mealie_token), out):
            print(f"wrote {p}")
    else:
        sys.exit(f"unknown command {a.command!r}\n{__doc__}")


if __name__ == "__main__":
    main()

# mealie-hook

Finishes off recipes imported into a self-hosted [Mealie](https://mealie.io) instance, as
soon as they are imported. The work was previously done by hand through a Claude Code skill;
this service does it in code, with the judgement calls made by the local Qwen model on
marvin's llama-server.

For every recipe imported from a URL it:

1. **Checks the scrape**: no ingredients, no method, placeholder text, or a step that stops
   mid-sentence. It rebuilds a broken import from the source page, using site-specific
   markup for deliciousmagazine.co.uk (which dropped its Recipe JSON-LD) and JSON-LD
   elsewhere.
2. **Fills nutrition** that Mealie missed, including saturated fat hidden inside the fat
   string (`"12.1g (2.1g saturated)"`), then shows the nutrition panel.
3. **Parses ingredients** into quantity / unit / food / note (Qwen). Section headings
   ("For the sauce") become Mealie section titles, and equipment rows are dropped.
4. **Matches every food and unit** onto existing Mealie records: plurals, `toasted sesame
   oil` → `sesame oil` + note, `litres` → `l`. A new food is created only when nothing
   matches, and never with prep words in its name.
5. **Files it**: one category, protein/season/character tags, and tools (Qwen), chosen only
   from Mealie's existing vocabulary. Rules in code: slow cooker ⇒ Winter, never both
   seasons, provenance tags never chosen.
6. **Labels new foods** with their shopping aisle and plural form (Qwen) before creating them.

Anything it is unsure of is written as far as it safely can, and the recipe gets a
**Needs review** tag plus a note saying why. Rows it could not parse are left as written.

## How it is triggered

Mealie emits `recipe_created` through its notifier system (Apprise). A `json://` notifier
POSTs it to this service. Mealie's event is only a trigger, never the source of truth:

- a single-URL import sends the slug;
- a **bulk** URL import sends one event carrying only a report id, and sends it *before*
  scraping starts. The service polls the report until it finishes.

Either way the service waits `SETTLE_SECONDS`, then sweeps: any recipe with an `orgURL`,
created since the service first started, and not yet marked processed. A periodic sweep
(`SWEEP_MINUTES`) catches lost events. Hand-written recipes (no `orgURL`) are never touched.

"Processed" is recorded in the recipe's `extras` (`mealie-hook: <version> <time>`), so it
survives a lost `data/` directory.

### Safety

- **Edit guard**: if the recipe's `dateUpdated` changes while the model is working (you
  opened it and saved), the write is abandoned and retried on the next sweep.
- **Undo**: the recipe as it was before the write is saved under
  `data/snapshots/<slug>/`, and `undo <slug>` puts it back.
- **Additive**: categories, tags and tools are only ever added. Already-parsed ingredient
  rows are never touched.
- **Retry limit**: after `MAX_ATTEMPTS` failures (for example llama-server down) the recipe
  is marked processed and tagged Needs review with the error.
- **No thinking tokens**: each request sends `chat_template_kwargs: {enable_thinking:
  false}`, so the shared llama-server keeps its defaults. Typical cost is ~20s for
  ingredients plus ~3s to classify.

## Layout

```
mealie_hook/
  __main__.py     CLI
  server.py       webhook receiver + worker (settle, bulk-report wait, periodic sweep)
  pipeline.py     per-recipe pipeline and sweep
  scrape.py       scrape checks, page extraction (BeautifulSoup), nutrition
  ingredients.py  model call + deterministic post-processing
  foods.py        food/unit matching, plurals, prep-word guard
  classify.py     category/tags/tools + house rules
  labels.py       aisle + plural for new foods
  models.py       pydantic models: the LLM JSON schemas and their validators
  evaluate.py     replay processed recipes and score agreement
  export.py       tracking JSON files from Mealie's state
  mealie.py llm.py config.py state.py
prompts/          system prompts, read on every call (editable without a restart)
tests/            offline tests (fake Mealie, fake model)
```

## Deploy (marvin)

One-time setup:

```bash
ssh homelab 'mkdir -p ~/compose/mealie-hook'
# create an API token in Mealie (Profile -> API Tokens, name "mealie-hook"), then:
ssh homelab 'cat > ~/compose/mealie-hook/.env && chmod 600 ~/compose/mealie-hook/.env' <<< 'MEALIE_TOKEN=...'
```

Then, from this directory, and again after every change:

```bash
./deploy.sh        # rsync -> podman build on marvin -> docker compose up -d (via Dockge)
```

Finally point Mealie's notifier at it (creates or updates one named `mealie-hook`, enabled,
firing on **Recipe Created** only):

```bash
ssh homelab 'podman exec mealie-hook python -m mealie_hook notifier --create'
```

(In the UI it lives under Settings → Household → Notifiers; `--delete` removes it.)

The container joins the `mealie_default` network. It publishes no port, and reaches
llama-server at `host.containers.internal:8080`.

## Operating

```bash
C='podman exec mealie-hook python -m mealie_hook'
ssh homelab "$C candidates"                    # what the next sweep would take
ssh homelab "$C process SLUG --dry"            # full pipeline, print result, write nothing
ssh homelab "$C process SLUG --force"          # (re)process one recipe now, even an old one
ssh homelab "$C undo SLUG"                     # restore the pre-edit snapshot
ssh homelab "$C eval 20"                       # agreement vs 20 already-filed recipes
ssh homelab "$C export /data/export"           # tracking JSON -> ~/compose/mealie-hook/data/export
ssh homelab 'podman logs -f mealie-hook'
ssh homelab 'podman exec mealie-hook python -c "import urllib.request as u;print(u.urlopen(\"http://localhost:8000/health\").read().decode())"'
```

Filter by the **Needs review** tag in Mealie to see what needs a human. Delete the tag
and the "Needs review (mealie-hook)" note once you have checked a recipe.

Set `DRY_RUN=true` in `.env` to run the whole thing on real events without writing.

## Measured (2026-09-26, Qwen3.5-35B-A3B Q8_0, thinking off)

Against 20 recipes already parsed and filed by hand (`eval 20`): **91%** of 222 ingredient
rows matched on every field (food 93%, unit 99%, quantity 99%, section titles 100%);
category **19/20**; tags precision 71%, recall 84%. Most food "misses" are synonym choices
(`potato` for `new potato`, `garlic powder` for `garlic granules`); seasons are the least
reliable tag. About 15-35s per recipe. End-to-end tested live, including recipes whose foods
were not in the database (new foods created with aisle and plural), bulk imports, and
the Apprise delivery.

Two Mealie/Apprise quirks the code handles, both found in live testing:

- `document_data` arrives URL-encoded (`{"reportId":+"..."}`), because Mealie encodes it into
  the Apprise URL and Apprise never decodes it.
- A bulk import's single event arrives before scraping starts; the service waits on the
  report (`GET /groups/reports/{id}`) before sweeping.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The prompts are the main tuning surface. After changing one, run `eval` to measure it
against the recipes already in the collection.

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
   from Mealie's existing vocabulary. Rules in code, keyed on roles from the rules file
   rather than on names: slow cooker ⇒ winter, never both seasons, provenance tags never
   chosen, character tags like Savoury never on a main.
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

## The page: recipes.<domain>/rules/

Served by this service, through Caddy, on Mealie's own domain. It uses your Mealie login
(the `mealie.access_token` cookie, checked against Mealie on each request) and is for
Mealie admins only.

- **Shopping**: the Mealie shopping list and Home Assistant's, merged into one list of
  shoppable names for pasting into a supermarket search. Home Assistant items a Mealie item
  already covers are crossed out. **Copy** puts the list on the clipboard. **Tick all
  off** ticks exactly the items shown in both places; anything added since the page loaded
  is left alone.
  Forgot to tick off after a shop? Set **added since** to that shop's date: Mealie items
  last added to before it move to a "probably bought already" group, drop out of the
  export, and get their own **Tick off these** button. An item topped up by a later recipe
  counts as new. Home Assistant items have no dates, so they always stay in the export. The
  page shows when you last ticked off, as a reminder.
- **Needs review**: flagged recipes with their reasons, linked into Mealie. **Mark
  reviewed** removes the tag and the note.
- **Rules**: every category, tag, tool and aisle with its guidance and roles, the prompts
  (plus "what Qwen sees" once the vocabulary is filled in), and mismatches with Mealie, e.g. a
  tag with no guidance, a vocabulary name Mealie lacks (with a **Create in Mealie**
  button), or a code rule disabled because its role has no live name. Files can also be
  edited by hand.
- **Change rules**: describe a change in plain words; Qwen proposes an edit, which is checked
  and shown as a diff. **Try on recipe** runs it on a filed recipe without writing
  anything; **Apply** saves it. New names come with a one-click "create in Mealie"; nothing
  is ever renamed or deleted in Mealie.
- **History**: every change, with its diff and **Revert to before this**. A revert is
  itself recorded.

### The rules

`rules/` in this repo holds the shipped defaults. On first start they are copied to
`data/rules/`, which is the live copy the page edits. The service reads it on every recipe,
so edits apply immediately, with no redeploy, and image rebuilds never overwrite it.

- `vocabulary.toml`: guidance and roles for each Mealie organizer. Mealie supplies the
  names (the model can only choose names that exist); this file says what they mean.
- `classify.md`, `labels.md`, `ingredients.md`: the prompts. `{{categories}}`, `{{tags}}`,
  `{{tools}}` and `{{labels}}` are filled from the vocabulary and must stay.

## Layout

```
mealie_hook/
  __main__.py     CLI
  server.py       worker (settle, bulk-report wait, periodic sweep) + event parsing
  web.py          FastAPI: internal /hook, and the /ui page + API (Mealie-login auth)
  pipeline.py     per-recipe pipeline, sweep, and replay (try rules on a filed recipe)
  rules.py        vocabulary + prompt templates: store, render, drift, history
  chat.py         rules editing via Qwen: proposal -> validate -> diff
  editor.md       the rules editor's own instructions (not editable from the page)
  shopping.py     Mealie + Home Assistant shopping export and tick-off
  review.py       the Needs review queue
  scrape.py       scrape checks, page extraction (BeautifulSoup), nutrition
  ingredients.py  model call + deterministic post-processing
  foods.py        food/unit matching, plurals, prep-word guard
  classify.py     category/tags/tools + role-based rules
  labels.py       aisle + plural for new foods
  models.py       pydantic models: the LLM JSON schemas and their validators
  evaluate.py     replay processed recipes and score agreement
  export.py       tracking JSON files from Mealie's state
  ui/index.html   the page (no build step)
  mealie.py llm.py config.py state.py
rules/            shipped default rules
tests/            offline tests (fake Mealie, fake model, FastAPI test client)
```

## Deploy (marvin)

One-time setup:

```bash
ssh homelab 'mkdir -p ~/compose/mealie-hook'
# create an API token in Mealie (Profile -> API Tokens, name "mealie-hook"), then:
ssh homelab 'cat > ~/compose/mealie-hook/.env && chmod 600 ~/compose/mealie-hook/.env' <<< 'MEALIE_TOKEN=...'
```

For the Shopping tab, add Home Assistant to the same `.env` (optional):

```
HA_URL=https://home.example.com
HA_TOKEN=<long-lived access token>
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

The container joins the `mealie_default` network (for Mealie) and `proxy` (for Caddy). It
publishes no port, and reaches llama-server at `host.containers.internal:8080`.

Caddy serves the page on Mealie's domain; only `/ui` is exposed, never `/hook`:

```
recipes.example.com {
    redir /rules /rules/
    handle_path /rules/* {
        rewrite * /ui{path}
        reverse_proxy mealie-hook:8000
    }
    handle {
        reverse_proxy mealie:9000
    }
}
```

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

The rules are the main tuning surface: change them from the page, then run `eval` to measure
the effect against the recipes already in the collection.

"""Scrape quality: detect a broken import, repair it from the source page, fill nutrition.

A failed scrape is not always loud. Mealie reports only URLs that errored; a page that
returns 200 and yields a truncated method counts as a success. That is how a cod traybake
reached the meal plan with one half-finished step.
"""

import json
import re
import uuid
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Tag

NUTRITION_FIELDS = [
    "calories", "proteinContent", "fatContent", "saturatedFatContent",
    "carbohydrateContent", "sugarContent", "fiberContent", "sodiumContent",
    "cholesterolContent",
]

# Some sites bury a second figure in parentheses inside another field, e.g.
# "12.1g (2.1g saturated)" and "52.1g (17.3g sugars)". Mealie keeps only the leading
# number, so saturated fat -- the figure this collection is organised around -- arrives
# empty. An explicit field always beats one derived from a parenthetical.
NESTED_NUTRITION = {
    "fatContent": [(r"\(([\d.]+)\s*g?\s*saturate", "saturatedFatContent")],
    "carbohydrateContent": [(r"\(([\d.]+)\s*g?\s*sugar", "sugarContent")],
}

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120 Safari/537.36")

_END_PUNCT = ".!?)]”\"':"


def scrape_problems(recipe: dict) -> tuple[list[str], list[str]]:
    """Judge one recipe -> (hard failures, soft notes). Pure."""
    ings = recipe.get("recipeIngredient") or []
    steps = recipe.get("recipeInstructions") or []
    texts = [(i.get("originalText") or i.get("note") or "") for i in ings]
    stexts = [(s.get("text") or "").strip() for s in steps]

    why, soft = [], []
    if not ings:
        why.append("no ingredients")
    if not steps:
        why.append("no instructions")
    if any("could not detect" in t.lower() for t in texts + stexts):
        why.append("scraper placeholder text")
    if len(ings) == 1:
        why.append("only 1 ingredient row")
    for n, t in enumerate(stexts):
        if t and t[-1] not in _END_PUNCT:
            why.append(f"step {n + 1} ends mid-sentence: ...{t[-40:]!r}")
    if len(steps) == 1 and len(stexts[0]) < 120:
        soft.append("only one short step")
    if not recipe.get("recipeServings"):
        soft.append("no servings (normal for a batch bake with a yield)")
    return why, soft


def fetch_html(url: str, timeout: float = 30) -> str:
    # Some sources 403 anything that does not look like a browser.
    r = httpx.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout,
                  follow_redirects=True)
    r.raise_for_status()
    return r.text


def nutrition_from_ld(n: dict) -> dict:
    """schema.org nutrition dict -> Mealie nutrition dict, including nested figures."""
    out, derived = {}, {}
    for key in NUTRITION_FIELDS:
        raw = n.get(key)
        if raw is None:
            continue
        text = str(raw)
        hit = re.search(r"[\d.]+", text)
        if hit:
            out[key] = hit.group(0)
        for pattern, target in NESTED_NUTRITION.get(key, []):
            sub = re.search(pattern, text, re.I)
            if sub:
                derived[target] = sub.group(1)
    for k, v in derived.items():
        out.setdefault(k, v)
    return out


def ld_recipe(soup: BeautifulSoup) -> dict | None:
    """The Recipe object from the page's JSON-LD, looking inside @graph too."""
    found = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        stack = [d]
        while stack:
            c = stack.pop()
            if isinstance(c, list):
                stack.extend(c)
            elif isinstance(c, dict):
                if "Recipe" in str(c.get("@type", "")):
                    found = c
                if "@graph" in c:
                    stack.append(c["@graph"])
    return found


def _text(node) -> str:
    raw = node.get_text(" ") if isinstance(node, Tag) else BeautifulSoup(str(node), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", raw).strip()


def _drop_ads(section: Tag) -> Tag:
    for ad in section.select(".delicious-ad-wrapper, li.ad-li, [id^=adslot]"):
        ad.decompose()
    return section


def delicious_extract(soup: BeautifulSoup) -> dict:
    """deliciousmagazine.co.uk no longer publishes Recipe JSON-LD (since 2026-09).

    The recipe is still in the page as ordinary markup: #ingredients holds <ul> lists
    separated by <p><strong>For the ...</strong></p> section headings, #method an <ol> with
    an advertising pseudo-step, and #nutrition flat strings such as "8.2g fat (2.3g
    saturated)" beside a label.
    """
    lines, steps, nut_raw = [], [], {}
    ing = soup.find(id="ingredients")
    if ing:
        # headings and items, in document order
        lines = [t for t in (_text(n) for n in _drop_ads(ing).select("li, p")) if t]
    method = soup.find(id="method")
    if method:
        steps = [t for t in (_text(li) for li in _drop_ads(method).select("li"))
                 if t and "continues after advertising" not in t.lower()]
    labels = {"calories": "calories", "fat": "fatContent", "protein": "proteinContent",
              "carbs": "carbohydrateContent", "carbohydrate": "carbohydrateContent",
              "fibre": "fiberContent", "fiber": "fiberContent", "sugars": "sugarContent",
              "saturates": "saturatedFatContent", "salt": "sodiumContent"}
    nutrition = soup.find(id="nutrition")
    for li in nutrition.select("li") if nutrition else []:
        value, measure = li.select_one(".nutrition-unit"), li.select_one(".nutrition-measure")
        key = labels.get(_text(measure).lower()) if measure else None
        if key and value:
            nut_raw[key] = _text(value)
    m = re.search(r"Serves\s+(\d+)", soup.get_text(" "))
    # sodiumContent holds grams of SALT across this collection, as the sources publish it.
    return {"ingredients": lines, "instructions": steps,
            "nutrition": nutrition_from_ld(nut_raw), "servings": int(m.group(1)) if m else None}


def _ld_steps(instructions) -> list[str]:
    out = []
    for s in instructions if isinstance(instructions, list) else [instructions]:
        if isinstance(s, str):
            out.append(_text(s))
        elif isinstance(s, dict):
            if s.get("itemListElement"):
                out += _ld_steps(s["itemListElement"])
            elif s.get("text"):
                out.append(_text(s["text"]))
    return [s for s in out if s]


def extract_from_page(url: str, html: str) -> dict:
    """Best available recipe content from the page: site-specific first, then JSON-LD."""
    soup = BeautifulSoup(html, "html.parser")
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("deliciousmagazine.co.uk"):
        d = delicious_extract(soup)
        if d["ingredients"] or d["instructions"]:
            return d
    rec = ld_recipe(soup) or {}
    y = rec.get("recipeYield")
    m = re.search(r"\d+", str(y[0] if isinstance(y, list) and y else y or ""))
    return {
        "ingredients": [_text(t) for t in rec.get("recipeIngredient") or [] if str(t).strip()],
        "instructions": _ld_steps(rec.get("recipeInstructions") or []),
        "nutrition": nutrition_from_ld(rec.get("nutrition") or {}),
        "servings": int(m.group(0)) if m else None,
    }


def raw_ingredient(text: str) -> dict:
    return {"referenceId": str(uuid.uuid4()), "quantity": 0, "unit": None, "food": None,
            "note": text, "originalText": text, "title": None}


def raw_step(text: str) -> dict:
    return {"id": str(uuid.uuid4()), "title": "", "summary": "", "text": text,
            "ingredientReferences": []}


def repair(recipe: dict, page: dict) -> list[str]:
    """Replace broken ingredients/instructions with the page's. Mutates `recipe`.

    Returns what was changed. Only called on a freshly imported recipe that failed
    `scrape_problems`, so nothing a human wrote is overwritten.
    """
    changed = []
    ings, steps = page["ingredients"], page["instructions"]
    if len(ings) >= 2 and len(ings) >= len(recipe.get("recipeIngredient") or []):
        recipe["recipeIngredient"] = [raw_ingredient(t) for t in ings]
        changed.append(f"ingredients rebuilt from the source page ({len(ings)} lines)")
    if steps and all(s[-1] in _END_PUNCT for s in steps):
        recipe["recipeInstructions"] = [raw_step(t) for t in steps]
        changed.append(f"method rebuilt from the source page ({len(steps)} steps)")
    return changed


def fill_nutrition(recipe: dict, page_nutrition: dict) -> list[str]:
    """Fill empty nutrition fields only; never overwrite a figure. Mutates `recipe`."""
    n = dict(recipe.get("nutrition") or {})
    added = {k: v for k, v in page_nutrition.items() if k in NUTRITION_FIELDS and not n.get(k)}
    if added:
        n.update(added)
        recipe["nutrition"] = n
    return sorted(added)


def missing_nutrition(recipe: dict) -> bool:
    n = recipe.get("nutrition") or {}
    # cholesterol is never published by the main sources; do not fetch just for it
    return any(not n.get(f) for f in NUTRITION_FIELDS if f != "cholesterolContent")

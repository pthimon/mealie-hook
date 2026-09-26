"""Matching model output onto Mealie's existing food and unit records.

The food table is shared by every recipe and shopping lists merge on `food_id`, so a second
spelling of an existing food never sums on a list. Everything here errs towards reusing an
existing record, and refuses to create one that looks wrong.
"""

import re

# Canonical short form for unit spellings, so "2 cloves" and "100 grams" land on the `clove`
# and `g` records instead of creating duplicates (the ones that had crept in were merged in
# Mealie on 2026-09-26).
UNIT_ALIASES = {
    "gram": "g", "grams": "g", "gr": "g", "gms": "g",
    "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg",
    "millilitre": "ml", "millilitres": "ml", "milliliter": "ml", "milliliters": "ml",
    "litre": "l", "litres": "l", "liter": "l", "liters": "l", "ltr": "l",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsps": "tsp",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsps": "tbsp", "tbs": "tbsp",
    "dessertspoon": "dsp", "dessertspoons": "dsp",
    "cloves": "clove", "pinches": "pinch", "handfuls": "handful", "bunches": "bunch",
    "pieces": "piece", "knobs": "knob", "sprigs": "sprig", "sticks": "stick",
    "strips": "strip", "slices": "slice", "heads": "head", "stalks": "stalk",
    "bags": "bag", "packs": "pack", "packet": "pack", "packets": "pack", "sheets": "sheet",
    "squares": "square", "dashes": "dash", "cups": "cup", "tins": "tin", "cans": "can",
    "jars": "jar", "sachets": "sachet", "pots": "pot", "punnets": "punnet",
}

# Units the service may create if Mealie does not have them yet. Anything else the model
# proposes is moved into the note instead -- a unit is a shared record, like a food.
ALLOWED_UNITS = {
    "g", "kg", "ml", "l", "tsp", "tbsp", "dsp", "clove", "pinch", "handful", "bunch",
    "piece", "knob", "sprig", "stick", "strip", "slice", "head", "stalk", "bag", "pack",
    "sheet", "square", "dash", "cup", "tin", "can", "jar", "sachet", "pot", "punnet",
}

# Units written as abbreviations, the same for one or many ("2 tbsp", not "2 tbsps").
ABBREVIATED_UNITS = {"g", "kg", "ml", "l", "tsp", "tbsp", "dsp"}

# Plural for every other unit the service may create ("clove" -> "cloves"), taken from the
# alias table so the two cannot drift. Mealie shows it on lists and recipes for amounts over 1.
UNIT_PLURALS = {v: k for k, v in UNIT_ALIASES.items()
                if v in ALLOWED_UNITS and v not in ABBREVIATED_UNITS and k in (v + "s", v + "es")}

# "Vague measure" units that a line can open with and the model sometimes leaves in the note
# ("Handful fresh coriander" -> quantity 0, no unit). Recovered deterministically.
VAGUE_UNITS = ("handful", "pinch", "bunch", "knob", "sprig", "stick", "strip", "slice",
               "head", "stalk", "bag", "pack", "piece", "dash")
_VAGUE_RE = re.compile(
    r"^\s*(?:(?P<n>\d+(?:\.\d+)?|a|an|one)\s+)?"
    r"(?:(?P<size>[a-z]+-sized?|small|large|generous|big|little|good|decent)\s+)?"
    r"(?P<unit>" + "|".join(VAGUE_UNITS) + r")(?:e?s)?\b",
    re.I,
)

# Leading words that describe an ingredient without changing what you buy. Stripped from a
# food name ONLY when what remains is already an existing food, and moved to the note.
SAFE_MODIFIERS = {
    "fresh", "free-range", "organic", "large", "medium", "small", "ripe", "unwaxed",
    "good-quality", "good", "quality", "best", "best-quality", "toasted", "skinless",
    "boneless", "lean", "extra-large", "baby", "whole",
}

# Words that mean a prep step leaked into a food name. A new food containing one is never
# created; the row is left raw and flagged instead.
PREP_WORDS = (
    "chopped", "sliced", "crushed", "diced", "grated", "minced", "peeled", "trimmed",
    "drained", "rinsed", "halved", "quartered", "shredded", "cooked", "to serve",
    "to garnish", "for frying", "finely", "roughly", "thinly", "beaten", "softened",
    "melted", "zested", "juiced",
)
_PREP_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in PREP_WORDS) + r")\b", re.I)


_FRACTIONS = {"½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75, "⅛": 0.125,
              "⅜": 0.375, "⅝": 0.625, "⅞": 0.875}
_LEADING_QTY = re.compile(
    r"^\s*(?:(?P<whole>\d+(?:\.\d+)?)\s*)?"
    r"(?:(?P<uni>[" + "".join(_FRACTIONS) + r"])|(?P<num>\d+)/(?P<den>\d+))?")

# Trailing words a model tends to glue onto a herb ("sage leaves"). Stripped only when the
# remainder is an existing food in a herb aisle (role "herbs" in the vocabulary): "lime leaf"
# must not collapse into "lime".
HERB_SUFFIXES = ("leaf", "leaves")


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def loose(s: str | None) -> str:
    """Punctuation-insensitive key: "flat-leaf parsley" == "flat leaf parsley",
    "za'atar" == "za’atar". Same words, same food."""
    s = norm(s).replace("’", "'").replace("‘", "'").replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[-‐–]", " ", s)).strip()


def word_key(s: str | None) -> tuple[str, ...]:
    """Same words in any order: "soft light brown sugar" == "light brown soft sugar"."""
    return tuple(sorted(loose(s).split()))


def plural_candidates(name: str) -> set[str]:
    """Every plausible plural spelling of `name`, lowercased.

    Deliberately over-generates rather than picking one "correct" plural. A stemmer that
    singularised instead turned "oranges" into "orang" and "chillies" into "chilly", and so
    missed real duplicates.
    """
    n = norm(name)
    if not n:
        return set()

    def forms(word: str) -> set[str]:
        # The -ves rules are guarded on the ending: unguarded, "chips" becomes "chives".
        out = {word + "s", word + "es"}
        if word.endswith("y"):
            out.add(word[:-1] + "ies")
        if word.endswith("i"):                      # chilli -> chillies
            out.add(word[:-1] + "es")
        if word.endswith("f"):                      # leaf -> leaves
            out.add(word[:-1] + "ves")
        if word.endswith("fe"):                     # knife -> knives
            out.add(word[:-2] + "ves")
        return out

    head, _, last = n.rpartition(" ")
    prefix = f"{head} " if head else ""
    cands = forms(n) | {prefix + f for f in forms(last)}
    return {c for c in cands if c and c != n}


def find_duplicate_pairs(names: list[str]) -> list[tuple[str, str]]:
    """[(singular, plural)] for names where both a form and its plural exist."""
    by_low = {norm(n): n for n in names}
    pairs = set()
    for low, original in by_low.items():
        for cand in plural_candidates(low):
            if cand in by_low:
                pairs.add((original, by_low[cand]))
    return sorted(pairs)


def leading_quantity(text: str) -> float | None:
    """The number a line opens with: "½ red onion" -> 0.5, "1 1/2 tsp" -> 1.5, "1½" -> 1.5.

    Used only when the model returned 0, which it does for unicode fractions.
    """
    bare = re.match(r"\s*(\d+)/(\d+)\b", text or "")           # "3/4 cup"
    if bare and int(bare.group(2)):
        return round(int(bare.group(1)) / int(bare.group(2)), 3) or None
    m = _LEADING_QTY.match(text or "")
    if not m or not any(m.group(g) for g in ("whole", "uni", "num")):
        return None
    total = float(m.group("whole") or 0)
    if m.group("uni"):
        total += _FRACTIONS[m.group("uni")]
    elif m.group("num") and int(m.group("den")):
        total += int(m.group("num")) / int(m.group("den"))
    return round(total, 3) or None


_LEADING_UNIT = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?|[" + "".join(_FRACTIONS) + r"]|\d+/\d+)"
    r"(?:\s*(?:[" + "".join(_FRACTIONS) + r"]|\d+/\d+))?\s*(?P<unit>[a-z]+)\b", re.I)


def leading_unit(text: str) -> str | None:
    """An explicit unit straight after the opening number: "2 tbsp ..." / "100g ...".

    Used only when the model returned no unit ("2 tbsp coriander leaves" came back unitless).
    """
    m = _LEADING_UNIT.match(text or "")
    if not m:
        return None
    u = canonical_unit(m.group("unit"))
    return u if u in ALLOWED_UNITS else None


def looks_prepped(food: str) -> bool:
    return bool(_PREP_RE.search(food)) or len(food) > 40


def canonical_unit(unit: str | None) -> str | None:
    u = norm(unit).rstrip(".")
    if not u:
        return None
    return UNIT_ALIASES.get(u, u)


def recover_vague_unit(text: str) -> tuple[str, float | None, str | None] | None:
    """If the raw line opens with a vague measure, return (unit, count or None, size word)."""
    m = _VAGUE_RE.match(text or "")
    if not m:
        return None
    n = m.group("n")
    count = None
    if n:
        count = 1.0 if n.lower() in ("a", "an", "one") else float(n)
    size = m.group("size").lower() if m.group("size") else None
    return m.group("unit").lower(), count, size


class Vocab:
    """Existing foods and units, indexed for lookup. Grows as new records are planned."""

    def __init__(self, foods: list[dict], units: list[dict], herb_labels=("Herbs & Spices",)):
        self.herb_labels = {norm(x) for x in herb_labels}
        self.foods: dict[str, dict] = {}
        self.loose_foods: dict[str, dict] = {}
        self.word_foods: dict[tuple, dict] = {}
        self.plural_to_food: dict[str, dict] = {}
        for f in foods:
            self.add_food(f)
        self.units: dict[str, dict] = {}
        for u in units:
            for key in (u.get("name"), u.get("pluralName"), u.get("abbreviation")):
                if key:
                    self.units.setdefault(norm(key), u)

    def add_food(self, f: dict):
        keys = [f.get("name"), f.get("pluralName")]
        keys += [a.get("name") for a in (f.get("aliases") or []) if isinstance(a, dict)]
        for key in keys:
            if key:
                self.foods.setdefault(norm(key), f)
                self.loose_foods.setdefault(loose(key), f)
                self.word_foods.setdefault(word_key(key), f)
        for cand in plural_candidates(f.get("name") or ""):
            self.plural_to_food.setdefault(cand, f)
            self.plural_to_food.setdefault(loose(cand), f)

    def food_names(self) -> list[str]:
        return sorted({f["name"] for f in self.foods.values()}, key=str.lower)

    def unit_names(self) -> list[str]:
        return sorted({u["name"] for u in self.units.values()}, key=str.lower)

    def unit(self, name: str | None) -> dict | None:
        c = canonical_unit(name)
        if not c:
            return None
        return self.units.get(c) or self.units.get(norm(name))

    def _lookup(self, name: str) -> dict | None:
        n = norm(name)
        if n in self.foods:
            return self.foods[n]
        if loose(n) in self.loose_foods:
            return self.loose_foods[loose(n)]
        if len(n.split()) > 1 and word_key(n) in self.word_foods:
            return self.word_foods[word_key(n)]
        # model wrote a plural of an existing singular record
        if n in self.plural_to_food or loose(n) in self.plural_to_food:
            return self.plural_to_food.get(n) or self.plural_to_food[loose(n)]
        # model wrote a singular of a record stored as a plural ("beansprout" -> "beansprouts")
        for cand in plural_candidates(n):
            if cand in self.foods:
                return self.foods[cand]
        return None

    def resolve_food(self, name: str) -> tuple[dict | None, list[str]]:
        """Return (existing record or None, modifier words stripped to reach it)."""
        rec = self._lookup(name)
        if rec:
            return rec, []
        words = norm(name).split()
        stripped = []
        while len(words) > 1 and words[0] in SAFE_MODIFIERS:
            stripped.append(words.pop(0))
            rec = self._lookup(" ".join(words))
            if rec:
                return rec, stripped
        words = norm(name).split()
        if len(words) > 1 and words[-1] in HERB_SUFFIXES:
            rec = self._lookup(" ".join(words[:-1]))
            if rec and norm((rec.get("label") or {}).get("name")) in self.herb_labels:
                return rec, ["leaves"]
        return None, []

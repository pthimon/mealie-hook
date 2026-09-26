import pytest

from mealie_hook.foods import (Vocab, canonical_unit, find_duplicate_pairs, leading_quantity,
                               leading_unit, looks_prepped, recover_vague_unit)

DUPLICATE_CASES = [
    # regressions -- both were missed by an earlier stemmer ("orang", "chilly")
    (["orange", "oranges"], [("orange", "oranges")]),
    (["red chilli", "red chillies"], [("red chilli", "red chillies")]),
    (["carrot", "carrots"], [("carrot", "carrots")]),
    (["bay leaf", "bay leaves"], [("bay leaf", "bay leaves")]),
    (["tomato", "tomatoes"], [("tomato", "tomatoes")]),
    (["cherry tomato", "cherry tomatoes"], [("cherry tomato", "cherry tomatoes")]),
    (["cherry", "cherries"], [("cherry", "cherries")]),
    # must NOT flag -- genuinely different foods
    (["chicken thigh", "chicken leg", "chicken breast"], []),
    (["onion", "red onion", "spring onion"], []),
    (["salmon", "salmon fillet"], []),
    (["lemon", "lemon juice", "lemon zest"], []),
    # regression: an unguarded -ves rule turned "chips" into "chives"
    (["chips", "chives"], []),
]


@pytest.mark.parametrize("names,expected", DUPLICATE_CASES)
def test_duplicate_pairs(names, expected):
    assert find_duplicate_pairs(names) == expected


FOODS = [{"name": n, "pluralName": p} for n, p in [
    ("carrot", "carrots"), ("sesame oil", None), ("beansprouts", None),
    ("low-salt soy sauce", None), ("red chilli", None), ("chicken thigh", None),
    ("egg", "eggs"), ("coriander", None),
]] + [{"name": "sage", "label": {"name": "Herbs & Spices"}},
      {"name": "lime", "label": {"name": "Fruit & Veg"}},
      {"name": "flat-leaf parsley"}, {"name": "za’atar"}, {"name": "rice noodles"},
      {"name": "light brown soft sugar"}]
UNITS = [{"name": n} for n in ["g", "tbsp", "tablespoon", "clove", "cloves", "handful", "l"]]


@pytest.fixture
def vocab():
    return Vocab(FOODS, UNITS)


@pytest.mark.parametrize("name,expected,stripped", [
    ("carrot", "carrot", []),
    ("Carrots", "carrot", []),                       # plural of an existing singular
    ("red chillies", "red chilli", []),
    ("beansprout", "beansprouts", []),               # singular of a record stored plural
    ("toasted sesame oil", "sesame oil", ["toasted"]),
    ("fresh coriander", "coriander", ["fresh"]),
    ("large free-range egg", "egg", ["large", "free-range"]),
])
def test_resolve_existing(vocab, name, expected, stripped):
    rec, words = vocab.resolve_food(name)
    assert rec["name"] == expected
    assert words == stripped


@pytest.mark.parametrize("name", ["chicken breast", "smoked salmon", "red onion",
                                  "soy sauce"])
def test_resolve_new(vocab, name):
    # different cuts/varieties are never folded onto a near neighbour
    assert vocab.resolve_food(name) == (None, [])


def test_modifier_strip_needs_an_existing_remainder(vocab):
    assert vocab.resolve_food("toasted hazelnut") == (None, [])


@pytest.mark.parametrize("raw,expected", [
    ("tablespoons", "tbsp"), ("Litres", "l"), ("cloves", "clove"), ("g", "g"),
    ("", None), (None, None), ("tsp.", "tsp"),
])
def test_canonical_unit(raw, expected):
    assert canonical_unit(raw) == expected


def test_unit_lookup_prefers_short_record(vocab):
    assert vocab.unit("tablespoon")["name"] == "tbsp"
    assert vocab.unit("cloves")["name"] == "clove"


@pytest.mark.parametrize("text,expected", [
    ("Handful fresh coriander leaves", ("handful", None, None)),
    ("1 thumb-size piece fresh ginger", ("piece", 1.0, "thumb-size")),
    ("small bunch parsley", ("bunch", None, "small")),
    ("a pinch of salt", ("pinch", 1.0, None)),
    ("2 sprigs rosemary", ("sprig", 2.0, None)),
    ("200g flour", None),
    ("1 red onion", None),
])
def test_recover_vague_unit(text, expected):
    assert recover_vague_unit(text) == expected


@pytest.mark.parametrize("food,bad", [
    ("chopped tomatoes", True),   # a real product, but a new food with it is still flagged
    ("onion finely sliced", True), ("carrot", False), ("x" * 41, True),
])
def test_looks_prepped(food, bad):
    assert looks_prepped(food) is bad


@pytest.mark.parametrize("name,expected", [
    ("sage leaves", ("sage", ["leaves"])),
    ("sage leaf", ("sage", ["leaves"])),
    ("lime leaf", (None, [])),            # a different thing entirely
    ("bay leaf", (None, [])),
])
def test_herb_leaves(vocab, name, expected):
    rec, words = vocab.resolve_food(name)
    assert ((rec or {}).get("name"), words) == expected


@pytest.mark.parametrize("text,expected", [
    ("½ red onion", 0.5), ("1½ tsp salt", 1.5), ("1 1/2 tbsp oil", 1.5), ("3/4 cup", 0.75),
    ("2 carrots", 2.0), ("0.5 lime", 0.5), ("red onion", None), ("salt, to taste", None),
])
def test_leading_quantity(text, expected):
    assert leading_quantity(text) == expected


@pytest.mark.parametrize("name,expected", [
    ("flat leaf parsley", "flat-leaf parsley"),      # would have made a duplicate food
    ("za'atar", "za’atar"),
    ("rice noodle", "rice noodles"),
    ("soft light brown sugar", "light brown soft sugar"),   # same words, other order
])
def test_punctuation_insensitive_match(vocab, name, expected):
    assert vocab.resolve_food(name)[0]["name"] == expected


@pytest.mark.parametrize("text,expected", [
    ("2 tbsp coriander leaves", "tbsp"), ("100g flour", "g"), ("1 ½kg steak", "kg"),
    ("2 garlic cloves", None), ("2 large eggs", None), ("3 sticks celery", "stick"),
    ("2 x 400g cans", None),
])
def test_leading_unit(text, expected):
    assert leading_unit(text) == expected

import pytest
from pydantic import ValidationError

from mealie_hook.foods import Vocab
from mealie_hook.ingredients import NEW, apply_rows, new_records, resolve_row, unparsed_lines
from mealie_hook.models import IngredientRow, ingredient_reply

FOODS = [{"id": f"f-{n}", "name": n} for n in
         ["carrot", "sesame oil", "coriander", "garlic", "ginger", "chicken stock"]]
UNITS = [{"id": f"u-{n}", "name": n} for n in ["g", "tbsp", "clove", "handful", "piece", "l"]]


def row(i, kind="ingredient", q=0, unit=None, food="", note=""):
    return IngredientRow(i=i, kind=kind, quantity=q, unit=unit, food=food, note=note)


def raw(text):
    return {"note": text, "originalText": text, "food": None, "unit": None, "quantity": 0}


@pytest.fixture
def vocab():
    return Vocab(FOODS, UNITS)


def test_handful_recovered_when_model_drops_it(vocab):
    # what Qwen actually returned in testing
    r = resolve_row(row(0, food="coriander", note="handful, fresh, leaves"),
                    "Handful fresh coriander leaves", vocab)
    assert (r.quantity, r.unit["name"], r.food["name"]) == (1.0, "handful", "coriander")
    assert r.note == "fresh, leaves"


def test_thumb_piece_recovered(vocab):
    r = resolve_row(row(0, q=1, food="ginger", note="thumb-size piece, fresh, thinly sliced"),
                    "1 thumb-size piece fresh ginger, thinly sliced", vocab)
    assert r.unit["name"] == "piece" and r.quantity == 1
    assert r.note == "thumb-size, fresh, thinly sliced"


def test_modifier_moves_to_note(vocab):
    r = resolve_row(row(0, q=1, unit="tbsp", food="toasted sesame oil"),
                    "1 tbsp toasted sesame oil", vocab)
    assert r.food["name"] == "sesame oil" and r.note == "toasted"


def test_unit_alias_maps_onto_existing(vocab):
    r = resolve_row(row(0, q=1.5, unit="litres", food="chicken stock"),
                    "1.5 litres chicken stock", vocab)
    assert r.unit["id"] == "u-l"


def test_allowed_new_unit_is_planned(vocab):
    r = resolve_row(row(0, q=1, unit="tin", food="chickpeas"), "1 tin chickpeas", vocab)
    assert r.unit == {"name": "tin", NEW: True}


def test_unknown_unit_goes_to_note_and_flags(vocab):
    r = resolve_row(row(0, q=2, unit="glugs", food="sesame oil"), "2 glugs sesame oil", vocab)
    assert r.unit is None and r.note.startswith("glugs") and r.flags


def test_prepped_new_food_left_raw(vocab):
    r = resolve_row(row(0, q=1, food="onion, finely chopped"), "1 onion, finely chopped", vocab)
    assert r.food is None and r.flags


def test_empty_food_left_raw(vocab):
    assert resolve_row(row(0), "salt", vocab).food is None


def test_new_food_is_reused_by_later_rows(vocab):
    a = resolve_row(row(0, q=2, food="parsnip"), "2 parsnips", vocab)
    b = resolve_row(row(1, q=1, food="parsnips"), "1 parsnip", vocab)
    assert a.food is b.food


def test_negative_quantity_clamped():
    assert row(0, q=-3).quantity == 0.0


def test_blank_unit_is_none():
    assert row(0, unit="  ").unit is None


def test_reply_model_pins_row_count():
    model = ingredient_reply(2)
    with pytest.raises(ValidationError):
        model.model_validate({"rows": [row(0).model_dump()]})
    assert model.model_json_schema()["properties"]["rows"]["minItems"] == 2


def test_headings_become_titles_and_equipment_is_dropped(vocab):
    ings = [raw("2 carrots"), raw("For the broth"), raw("1 garlic clove"),
            raw("You'll also need"), raw("Large casserole"), raw("1 tbsp sesame oil")]
    lines = unparsed_lines(ings)
    rows = [row(0, q=2, food="carrot"), row(1, "heading"), row(2, q=1, unit="clove", food="garlic"),
            row(3, "heading"), row(4, "equipment"), row(5, q=1, unit="tbsp", food="sesame oil")]
    pr = apply_rows(ings, lines, rows, vocab)
    assert [i["food"]["name"] for i in pr.ingredients] == ["carrot", "garlic", "sesame oil"]
    assert [i.get("title") for i in pr.ingredients] == [None, "For the broth", None]
    assert pr.equipment == ["Large casserole"]
    assert pr.parsed == 3 and not pr.flags


def test_already_parsed_rows_untouched(vocab):
    done = {"note": "", "originalText": "1 carrot", "food": FOODS[0], "quantity": 1}
    ings = [raw("For the top"), done]
    pr = apply_rows(ings, unparsed_lines(ings), [row(0, "heading")], vocab)
    assert pr.ingredients[0]["food"] is FOODS[0]
    assert pr.ingredients[0]["title"] == "For the top"


def test_trailing_heading_flagged(vocab):
    ings = [raw("1 carrot"), raw("To serve")]
    pr = apply_rows(ings, unparsed_lines(ings), [row(0, q=1, food="carrot"), row(1, "heading")], vocab)
    assert len(pr.ingredients) == 1 and pr.flags


def test_new_records_listed_once(vocab):
    ings = [raw("2 parsnips"), raw("1 parsnip"), raw("1 tin beans")]
    rows = [row(0, q=2, food="parsnip"), row(1, q=1, food="parsnip"),
            row(2, q=1, unit="tin", food="butter beans")]
    pr = apply_rows(ings, unparsed_lines(ings), rows, vocab)
    assert new_records(pr.ingredients) == (["parsnip", "butter beans"], ["tin"])


def test_input_not_mutated(vocab):
    ings = [raw("2 carrots")]
    apply_rows(ings, unparsed_lines(ings), [row(0, q=2, food="carrot")], vocab)
    assert ings[0]["food"] is None


def test_zero_quantity_filled_from_unicode_fraction(vocab):
    r = resolve_row(row(0, food="carrot"), "½ carrot", vocab)   # what Qwen returned
    assert r.quantity == 0.5


def test_no_quantity_line_stays_zero(vocab):
    assert resolve_row(row(0, food="carrot", note="to serve"), "carrot, to serve",
                       vocab).quantity == 0


def test_explicit_unit_recovered_when_model_drops_it(vocab):
    r = resolve_row(row(0, q=2, food="coriander", note="leaves"), "2 tbsp coriander leaves", vocab)
    assert r.unit["name"] == "tbsp" and r.note == "leaves"


def test_invalid_model_unit_recovered_from_line(vocab):
    r = resolve_row(row(0, q=2, unit="leaves", food="coriander"), "2 tbsp coriander leaves", vocab)
    assert r.unit["name"] == "tbsp" and not r.flags


def test_line_number_wins_when_units_agree(vocab):
    r = resolve_row(row(0, q=2, unit="stalk", food="lemongrass"), "3 stalks lemongrass", vocab)
    assert r.quantity == 3


def test_multiplied_cans_keep_model_total(vocab):
    r = resolve_row(row(0, q=800, unit="g", food="chopped tomatoes", note="2 x 400g cans"),
                    "2 x 400g cans chopped tomatoes", vocab)
    assert r.quantity == 800


class FlakyLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def structured(self, messages, reply, name, **_):
        return reply.model_validate(self.replies.pop(0))


def test_row_order_mismatch_retried(vocab):
    from mealie_hook.ingredients import call_model
    bad = {"rows": [row(1, q=1, food="carrot").model_dump(), row(1, q=1, food="garlic").model_dump()]}
    good = {"rows": [row(0, q=1, food="carrot").model_dump(), row(1, q=1, food="garlic").model_dump()]}
    rows = call_model(FlakyLLM([bad, good]), "", [(0, "1 carrot"), (1, "1 garlic")], vocab)
    assert [r.i for r in rows] == [0, 1]

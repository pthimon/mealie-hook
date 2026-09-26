"""Ingredient parsing: the model proposes, the code checks and restructures.

One model call per recipe classifies every unparsed row (ingredient / heading / equipment)
and splits ingredients into quantity, unit, food and note. Everything after that is
deterministic: headings move onto the next ingredient's `title`, equipment rows are dropped
(and offered to the tool classifier), and each food and unit is matched onto an existing
Mealie record where one exists.
"""

import json
import re
from dataclasses import dataclass, field

from .foods import (ALLOWED_UNITS, Vocab, canonical_unit, leading_quantity, leading_unit,
                    looks_prepped, norm, recover_vague_unit)
from .llm import LLM
from .models import IngredientRow, ingredient_reply

NEW = "_new"  # marks a food/unit dict that does not exist in Mealie yet


def raw_text(ing: dict) -> str:
    return (ing.get("originalText") or ing.get("note") or ing.get("display") or "").strip()


def unparsed_lines(ingredients: list[dict]) -> list[tuple[int, str]]:
    """(index, text) for every row with no food yet. Parsed rows are never touched."""
    return [(i, raw_text(ing)) for i, ing in enumerate(ingredients)
            if ing.get("food") is None and raw_text(ing)]


def build_messages(system: str, lines: list[tuple[int, str]], vocab: Vocab) -> list[dict]:
    # Rules and vocabulary first and identical across recipes, so llama-server's prompt
    # cache reuses them; only the final message changes per recipe.
    reference = (
        "Existing foods -- when a line is the same ingredient as one of these, use this exact "
        "spelling as `food`:\n" + "; ".join(vocab.food_names())
        + "\n\nExisting units: " + ", ".join(vocab.unit_names())
    )
    payload = [{"i": i, "text": t} for i, t in lines]
    return [
        {"role": "system", "content": system + "\n\n" + reference},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def call_model(llm: LLM, system: str, lines: list[tuple[int, str]],
               vocab: Vocab) -> list[IngredientRow]:
    err = None
    for _ in range(2):          # a mislabelled index is rare and random; one retry fixes it
        reply = llm.structured(build_messages(system, lines, vocab),
                               ingredient_reply(len(lines)), name="ingredients")
        bad = [(i, row.i) for (i, _), row in zip(lines, reply.rows) if row.i != i]
        if not bad:
            return reply.rows
        err = ValueError(f"row order mismatch: expected i={bad[0][0]}, got i={bad[0][1]}")
    raise err


@dataclass
class Resolved:
    quantity: float
    unit: dict | None
    food: dict | None      # None means "leave this row raw"
    note: str
    flags: list[str] = field(default_factory=list)


def _clean_note(parts: list[str]) -> str:
    seen, out = set(), []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip(" ,;")
        if p and norm(p) not in seen:
            seen.add(norm(p))
            out.append(p)
    return ", ".join(out)


def resolve_row(row: IngredientRow, text: str, vocab: Vocab) -> Resolved:
    """Turn one model row into Mealie field values. Pure apart from reading `vocab`."""
    flags = []
    qty = row.quantity
    unit_name = canonical_unit(row.unit)
    note_parts = row.note.split(",")
    bad_unit = None
    if unit_name and vocab.unit(unit_name) is None and unit_name not in ALLOWED_UNITS:
        # "2 tbsp coriander leaves" came back with unit "leaves": set it aside and recover
        # the unit from the line itself before giving up on it.
        bad_unit, unit_name = unit_name, None

    # "Handful fresh coriander" -> 1 handful, not 0 with the measure lost in the note.
    if unit_name is None:
        vague = recover_vague_unit(text)
        if vague:
            unit_name, count, size = vague
            if count is not None:
                qty = count
            elif qty == 0:
                qty = 1.0
            drop = {unit_name, unit_name + "s", unit_name + "es"}
            if size:
                drop |= {f"{size} {u}" for u in list(drop)}
            note_parts = [p for p in note_parts if norm(p) not in drop]
            if size and norm(size) not in {norm(p) for p in note_parts}:
                note_parts.insert(0, size)
        else:
            unit_name = leading_unit(text)
            if unit_name:
                note_parts = [p for p in note_parts if canonical_unit(p) != unit_name]
    if bad_unit and unit_name is None:
        flags.append(f"unknown unit {bad_unit!r} moved to the note: {text!r}")
        note_parts.insert(0, bad_unit)

    # Qwen returns 0 for "½ red onion"; the number is right there in the line.
    lead = leading_quantity(text)
    if qty == 0:
        qty = lead or 0.0
    # "3 stalks lemongrass" once came back as 2 stalks. When the line opens with a number
    # followed by the very unit the model chose, the line is right. ("2 x 400g cans" opens
    # with "2 x", so its 800 g is left alone.)
    elif lead and unit_name and leading_unit(text) == unit_name and qty != lead:
        qty = lead

    unit = None
    if unit_name:
        unit = vocab.unit(unit_name)
        if unit is None:
            unit = {"name": unit_name, NEW: True}

    food_name = norm(row.food).strip(" .,;:")
    if not food_name:
        flags.append(f"no food found, left as written: {text!r}")
        return Resolved(qty, unit, None, _clean_note(note_parts), flags)

    rec, stripped = vocab.resolve_food(food_name)
    if rec is None:
        if looks_prepped(food_name):
            flags.append(f"food {food_name!r} looks like it contains prep words, "
                         f"left as written: {text!r}")
            return Resolved(qty, unit, None, _clean_note(note_parts), flags)
        rec = {"name": food_name, NEW: True}
        vocab.add_food(rec)          # later rows in this run reuse the same new record
    elif stripped:
        note_parts = stripped + note_parts
    return Resolved(qty, unit, rec, _clean_note(note_parts), flags)


def _heading_text(text: str) -> str:
    t = re.sub(r"<[^>]+>", "", text).strip().strip("*_ ").rstrip(":").strip()
    return t[:1].upper() + t[1:] if t else t


@dataclass
class ParseResult:
    ingredients: list[dict]
    equipment: list[str]
    flags: list[str]
    parsed: int
    left_raw: int


def apply_rows(ingredients: list[dict], lines: list[tuple[int, str]],
               rows: list[IngredientRow], vocab: Vocab) -> ParseResult:
    """Build the new ingredient list. Does not mutate `ingredients`."""
    by_index = {i: row for (i, _), row in zip(lines, rows)}
    out, equipment, flags = [], [], []
    pending_title = None
    parsed = left_raw = 0

    for idx, original in enumerate(ingredients):
        ing = dict(original)
        text = raw_text(ing)
        row = by_index.get(idx)
        if row is None:                      # already parsed before we got here
            if pending_title and not ing.get("title"):
                ing["title"] = pending_title
            pending_title = None
            out.append(ing)
            continue

        kind = row.kind
        if kind == "heading":
            if pending_title:
                flags.append(f"heading {pending_title!r} had no ingredients under it; dropped")
            pending_title = _heading_text(text)
            continue
        if kind == "equipment":
            equipment.append(text)
            # a "You'll also need" heading belongs to the kit, not to the next ingredient
            pending_title = None
            continue

        r = resolve_row(row, text, vocab)
        flags += r.flags
        if pending_title:
            ing["title"] = pending_title
            pending_title = None
        if r.food is None:
            left_raw += 1
            out.append(ing)
            continue
        ing.update(quantity=r.quantity, unit=r.unit, food=r.food, note=r.note,
                   originalText=text)
        ing.pop("display", None)             # Mealie recomputes it
        parsed += 1
        out.append(ing)

    if pending_title:
        flags.append(f"heading {pending_title!r} at the end of the list; dropped")
    return ParseResult(out, equipment, flags, parsed, left_raw)


def new_records(ingredients: list[dict]) -> tuple[list[str], list[str]]:
    """Names of foods and units that must be created before the recipe is written."""
    foods, units = {}, {}
    for ing in ingredients:
        f, u = ing.get("food"), ing.get("unit")
        if isinstance(f, dict) and f.get(NEW):
            foods.setdefault(norm(f["name"]), f["name"])
        if isinstance(u, dict) and u.get(NEW):
            units.setdefault(norm(u["name"]), u["name"])
    return list(foods.values()), list(units.values())

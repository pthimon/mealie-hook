"""Aisle label and plural form for each food the parse is about to create."""

import json

from .llm import LLM
from .models import label_reply


def call_model(llm: LLM, system: str, foods: list[str], labels: list[str],
               examples: dict[str, list[str]]) -> dict[str, dict]:
    """-> {food: {"label": str, "plural": str | None}} for every food asked about."""
    ref = "\n".join(f"- {label}: {', '.join(names[:12])}" for label, names in examples.items())
    reply = llm.structured(
        [{"role": "system",
          "content": system + "\n\nExisting foods by aisle, for reference:\n" + ref},
         {"role": "user", "content": json.dumps(foods, ensure_ascii=False)}],
        label_reply(foods, labels), name="labels")
    out = {}
    for item in reply.foods:
        if item.food in out:
            continue
        plural = item.plural.strip().lower()
        # Only countable foods get a plural; mass nouns and collectives keep one name.
        if not item.countable or not plural or plural == item.food.lower():
            plural = None
        out[item.food] = {"label": item.label, "plural": plural}
    return out

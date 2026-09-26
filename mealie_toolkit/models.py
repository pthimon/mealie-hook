"""Pydantic models for everything the model returns and everything the service records.

Each model is both the JSON schema sent to llama-server (which compiles it to a grammar, so
the reply cannot be malformed) and the validator for the reply, so the two cannot drift.

Mealie's own recipe objects are deliberately NOT modelled. A recipe is read, edited and PUT
back whole; a model that did not list every field would silently drop the ones it lacked.
"""

from typing import Literal

from pydantic import BaseModel, Field, create_model, field_validator


# ----------------------------------------------------------------- ingredient parse

class IngredientRow(BaseModel):
    i: int
    kind: Literal["ingredient", "heading", "equipment"]
    quantity: float
    unit: str | None
    food: str
    note: str

    @field_validator("quantity", mode="after")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        return 0.0 if v != v or v < 0 else v          # NaN or negative -> 0

    @field_validator("unit", mode="after")
    @classmethod
    def _blank_unit_is_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v is not None else None


def ingredient_reply(n: int) -> type[BaseModel]:
    """Reply model with the row count pinned: a dropped or merged line cannot be generated."""
    return create_model(
        "IngredientReply",
        rows=(list[IngredientRow], Field(min_length=n, max_length=n)),
    )


# ------------------------------------------------------------------- classification

def _choice(names: list[str]):
    return Literal[tuple(names)]  # type: ignore[valid-type]


def classification_reply(categories: list[str], tags: list[str],
                         tools: list[str]) -> type[BaseModel]:
    """Built from Mealie's live organizers, so only existing names can be chosen."""
    return create_model(
        "ClassificationReply",
        # A short free-text description first gives a non-thinking model somewhere to
        # reason before it commits to the enum fields that follow.
        dish=(str, Field(max_length=300)),
        category=(_choice(categories), ...),
        # Free text, and never applied as-is: a protein with no existing tag is surfaced
        # for review rather than created, so the tag vocabulary cannot sprout near-duplicates.
        new_protein=(str | None, Field(default=None, max_length=30)),
        tags=(list[_choice(tags)] if tags else list[str], Field(max_length=6 if tags else 0)),
        tools=(list[_choice(tools)] if tools else list[str], Field(max_length=4 if tools else 0)),
    )


# --------------------------------------------------------------------- aisle labels

def label_reply(foods: list[str], labels: list[str]) -> type[BaseModel]:
    item = create_model(
        "FoodLabel",
        food=(_choice(foods), ...),
        label=(_choice(labels), ...),
        countable=(bool, ...),
        plural=(str, ...),
    )
    return create_model(
        "LabelReply",
        foods=(list[item], Field(min_length=len(foods), max_length=len(foods))),
    )


# ------------------------------------------------------------------ service records

class AppriseEvent(BaseModel):
    """What Mealie's `json://` notifier POSTs. Mealie adds its fields as `:key` params,
    which Apprise lifts into the top level of the payload; `document_data` is a JSON
    string, not an object."""
    title: str = ""
    message: str = ""
    event_type: str = ""
    integration_id: str = ""
    document_data: str = "{}"
    event_id: str = ""
    timestamp: str = ""


class Result(BaseModel):
    slug: str
    status: str = ""          # written | dry-run | skipped | deferred | failed
    reason: str = ""
    changes: list[str] = []
    flags: list[str] = []
    new_foods: list[str] = []
    new_units: list[str] = []
    category: str | None = None
    tags: list[str] = []
    tools: list[str] = []
    preview: list[str] = []
    seconds: float = 0.0

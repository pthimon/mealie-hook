import pytest
from pydantic import ValidationError

from mealie_hook.classify import Classification, apply_rules
from mealie_hook.models import classification_reply
from mealie_hook.rules import Vocabulary

VOCAB = Vocabulary.model_validate({
    "categories": {"Dinner": {"roles": ["main"]}, "Lunch": {"roles": ["main"]}},
    "tags": {"Summer": {"roles": ["season", "summer"]}, "Winter": {"roles": ["season", "winter"]},
             "Savoury": {"roles": ["character", "not-on-mains"]},
             "Dairy": {"roles": ["character", "not-on-mains"]},
             "Curry": {"roles": ["character"]},
             "Food for Life Cookbook": {"roles": ["provenance"]}},
    "tools": {"Slow Cooker": {"roles": ["slow-cooker"]}},
})

TAGS = {"Chicken", "Summer", "Winter", "Food for Life Cookbook", "Beans", "Savoury", "Dairy",
        "Curry", "Prawn", "White fish"}
TOOLS = {"Slow Cooker", "Casserole dish"}
BANNED = {"Food for Life Cookbook", "Needs review"}


def recipe(name="Curry", method="Simmer."):
    return {"name": name, "recipeIngredient": [], "recipeInstructions": [{"text": method}]}


def rules(tags, tools=(), **kw):
    return apply_rules(Classification("Dinner", list(tags), list(tools)), recipe(**kw),
                       TAGS, TOOLS, BANNED, VOCAB)


def test_slow_cooker_adds_tool_and_winter():
    c = rules(["Chicken", "Summer"], method="Cook in the slow cooker on low for 6 hours.")
    assert c.tools == ["Slow Cooker"] and "Winter" in c.tags and "Summer" not in c.tags


def test_slow_cooked_in_oven_is_not_slow_cooker():
    # slow-cooked-chipotle-chicken is a casserole-dish recipe, not a Slow Cooker one
    c = rules(["Chicken"], name="Slow-cooked chipotle chicken", method="Cook in the oven.")
    assert c.tools == []


def test_both_seasons_dropped_and_flagged():
    c = rules(["Summer", "Winter", "Chicken"])
    assert c.tags == ["Chicken"] and c.flags


def test_provenance_and_unknown_tags_removed_and_deduped():
    c = rules(["Food for Life Cookbook", "Made up", "Chicken", "Chicken"])
    assert c.tags == ["Chicken"]


def test_reply_model_only_accepts_live_vocabulary():
    model = classification_reply(["Dinner", "Lunch"], ["Chicken"], [])
    ok = model.model_validate({"dish": "x", "category": "Dinner", "tags": ["Chicken"],
                               "tools": []})
    assert ok.category == "Dinner"
    with pytest.raises(ValidationError):
        model.model_validate({"dish": "x", "category": "Supper", "tags": [], "tools": []})
    with pytest.raises(ValidationError):
        model.model_validate({"dish": "x", "category": "Lunch", "tags": [], "tools": ["Wok"]})


def test_character_tags_dropped_on_mains():
    c = rules(["Chicken", "Savoury", "Dairy", "Curry"])            # category Dinner
    assert c.tags == ["Chicken", "Curry"]


def test_character_tags_kept_on_breakfast():
    c = apply_rules(Classification("Breakfast", ["Savoury", "Dairy"], []), recipe(),
                    TAGS, TOOLS, BANNED, VOCAB)
    assert c.tags == ["Savoury", "Dairy"]


def test_unknown_protein_flagged_not_created():
    c = apply_rules(Classification("Dinner", [], [], new_protein="duck"), recipe(),
                    TAGS, TOOLS, BANNED, VOCAB)
    assert c.tags == [] and "'Duck'" in c.flags[0]


@pytest.mark.parametrize("proposal,tag", [("prawns", "Prawn"), ("CHICKEN", "Chicken"),
                                          ("white-fish", "White fish")])
def test_proposed_protein_matching_existing_tag_is_used(proposal, tag):
    c = apply_rules(Classification("Dinner", [], [], new_protein=proposal), recipe(),
                    TAGS, TOOLS, BANNED, VOCAB)
    assert c.tags == [tag] and not c.flags


def test_new_protein_ignored_off_mains():
    c = apply_rules(Classification("Dessert", [], [], new_protein="rhubarb"), recipe(),
                    TAGS, TOOLS, BANNED, VOCAB)
    assert not c.flags


def test_rules_follow_roles_not_names():
    # rename the season tags: the both-seasons rule must still apply
    v = Vocabulary.model_validate({"tags": {"Hot": {"roles": ["summer"]},
                                            "Cold": {"roles": ["winter"]}}})
    c = apply_rules(Classification("Dinner", ["Hot", "Cold"], []), recipe(),
                    {"Hot", "Cold"}, set(), set(), v)
    assert c.tags == [] and c.flags


def test_rule_without_live_role_is_inert():
    v = Vocabulary()                                  # no roles at all
    c = apply_rules(Classification("Dinner", ["Chicken"], []),
                    recipe(method="Use the slow cooker."), TAGS, TOOLS, BANNED, v)
    assert c.tools == [] and c.tags == ["Chicken"]

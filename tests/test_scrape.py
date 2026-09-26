from pathlib import Path

from mealie_toolkit.scrape import (extract_from_page, fill_nutrition, nutrition_from_ld, repair,
                                scrape_problems)

FIXTURE = (Path(__file__).parent / "fixtures" / "delicious_sample.html").read_text()
URL = "https://www.deliciousmagazine.co.uk/recipes/example-stew/"


def step(t):
    return {"text": t}


def ing(t):
    return {"note": t}


def test_complete_recipe_passes():
    r = {"recipeIngredient": [ing("a"), ing("b")], "recipeInstructions": [step("Cook it all.")],
         "recipeServings": 2}
    assert scrape_problems(r) == ([], ["only one short step"])


def test_truncated_step_is_hard_failure():
    # the real one that reached the meal plan
    r = {"recipeIngredient": [ing("a"), ing("b")],
         "recipeInstructions": [step("In a roasting tin, toss the")], "recipeServings": 2}
    why, _ = scrape_problems(r)
    assert any("mid-sentence" in w for w in why)


def test_placeholder_is_hard_failure():
    r = {"recipeIngredient": [ing("Could not detect ingredients")],
         "recipeInstructions": [step("Could not detect instructions")]}
    why, soft = scrape_problems(r)
    assert "scraper placeholder text" in why and "only 1 ingredient row" in why
    assert any("servings" in s for s in soft)


def test_nested_saturates_and_sugars():
    n = nutrition_from_ld({"fatContent": "12.1g (2.1g saturated)",
                           "carbohydrateContent": "52.1g (17.3g sugars)"})
    assert n == {"fatContent": "12.1", "saturatedFatContent": "2.1",
                 "carbohydrateContent": "52.1", "sugarContent": "17.3"}


def test_explicit_field_beats_nested():
    n = nutrition_from_ld({"fatContent": "12g (2g saturated)", "saturatedFatContent": "3g"})
    assert n["saturatedFatContent"] == "3"


def test_delicious_page_extract():
    d = extract_from_page(URL, FIXTURE)
    assert len(d["ingredients"]) == 15                      # 14 items + 1 section heading
    assert d["ingredients"][10] == "For the herby topping"
    assert d["ingredients"][0] == "2 tbsp olive oil"
    assert len(d["instructions"]) == 4                      # advertising pseudo-step dropped
    assert not any("advertising" in s.lower() for s in d["instructions"])
    assert not any("Extradelicious" in s or "Extra delicious" in s for s in d["ingredients"])
    assert d["nutrition"]["saturatedFatContent"] == "1.9"
    assert d["nutrition"]["sugarContent"] == "12.3"
    assert d["nutrition"]["sodiumContent"] == "1.2"         # grams of salt, by convention
    assert d["servings"] == 4


def test_jsonld_graph_extract():
    html = """<script type="application/ld+json">{"@graph": [{"@type": "WebPage"},
      {"@type": "Recipe", "recipeIngredient": ["1 egg", "2 carrots"],
       "recipeInstructions": [{"@type": "HowToSection", "itemListElement": [
          {"@type": "HowToStep", "text": "Boil.</br>"}, {"@type": "HowToStep", "text": "Eat."}]}],
       "recipeYield": ["4 servings"], "nutrition": {"calories": "300 kcal"}}]}</script>"""
    d = extract_from_page("https://example.com/r", html)
    assert d["ingredients"] == ["1 egg", "2 carrots"]
    assert d["instructions"] == ["Boil.", "Eat."]
    assert d["servings"] == 4 and d["nutrition"] == {"calories": "300"}


def test_repair_replaces_broken_content():
    r = {"recipeIngredient": [ing("Could not detect ingredients")],
         "recipeInstructions": [step("Could not detect instructions")]}
    changed = repair(r, extract_from_page(URL, FIXTURE))
    assert len(changed) == 2
    assert len(r["recipeIngredient"]) == 15 and r["recipeIngredient"][0]["food"] is None
    assert scrape_problems(r)[0] == []


def test_repair_keeps_longer_existing_ingredients():
    r = {"recipeIngredient": [ing(str(i)) for i in range(30)], "recipeInstructions": []}
    repair(r, {"ingredients": ["a", "b"], "instructions": []})
    assert len(r["recipeIngredient"]) == 30


def test_fill_nutrition_never_overwrites():
    r = {"nutrition": {"calories": "100", "fatContent": None}}
    assert fill_nutrition(r, {"calories": "999", "fatContent": "5"}) == ["fatContent"]
    assert r["nutrition"] == {"calories": "100", "fatContent": "5"}

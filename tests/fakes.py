"""Test doubles shared across the test modules."""

import copy


def raw(text):
    return {"note": text, "originalText": text, "food": None, "unit": None, "quantity": 0}


def base_recipe():
    return {
        "slug": "test-stew", "name": "Test stew", "orgURL": "https://www.bbcgoodfood.com/x",
        "recipeServings": 4, "createdAt": "2030-01-01T00:00:00Z", "dateUpdated": "t1",
        "recipeIngredient": [raw("2 carrots"), raw("For the topping"), raw("1 parsnip")],
        "recipeInstructions": [{"text": "Cook everything."}],
        "nutrition": {k: "1" for k in ["calories", "proteinContent", "fatContent",
                                        "saturatedFatContent", "carbohydrateContent",
                                        "sugarContent", "fiberContent", "sodiumContent"]},
        "settings": {"showNutrition": False}, "tags": [], "tools": [], "recipeCategory": [],
        "notes": [], "extras": {},
    }


class FakeMealie:
    def __init__(self):
        self.recipes_by_slug = {"test-stew": base_recipe()}
        self._foods = [{"id": "f1", "name": "carrot", "label": {"name": "Fruit & Veg"}}]
        self._units = [{"id": "u1", "name": "g"}]
        self._tags = [{"id": "t1", "name": "Winter"}, {"id": "t2", "name": "Vegetables"},
                      {"id": "t3", "name": "Food for Life Cookbook"}]
        self.puts, self.created_foods, self.created_tags = [], [], []
        self.created_organizers = []
        self.shop_items = [
            {"id": "m1", "checked": False, "quantity": 2, "display": "2 carrots",
             "createdAt": "2026-09-01T09:00:00", "updatedAt": "2026-09-01T09:00:00",
             "food": {"name": "carrot", "pluralName": "carrots", "label": {"name": "Fruit & Veg"}}},
            {"id": "m2", "checked": False, "quantity": 400, "unit": {"name": "g"},
             # created before the cut-off but topped up after it: counts as new
             "createdAt": "2026-09-01T09:00:00", "updatedAt": "2026-09-08T18:00:00",
             "food": {"name": "passata"}},
            {"id": "m3", "checked": False, "quantity": 0, "note": "bin bags", "food": None,
             "createdAt": "2026-09-07T12:00:00"},
            {"id": "m4", "checked": True, "quantity": 1, "food": {"name": "egg"}},
        ]
        self.edit_during_processing = False

    def recipes(self):
        return list(self.recipes_by_slug.values())

    def recipe(self, slug):
        r = copy.deepcopy(self.recipes_by_slug[slug])
        if self.edit_during_processing and self.puts == [] and getattr(self, "_reads", 0) >= 1:
            r["dateUpdated"] = "t2"
        self._reads = getattr(self, "_reads", 0) + 1
        return r

    def put_recipe(self, slug, data):
        self.puts.append(copy.deepcopy(data))
        self.recipes_by_slug[slug] = data

    def foods(self):
        return self._foods

    def units(self):
        return self._units

    def labels(self):
        return [{"id": "l1", "name": "Fruit & Veg"}, {"id": "l2", "name": "Dry Goods"}]

    def categories(self):
        return [{"id": "c1", "name": "Dinner"}, {"id": "c2", "name": "Lunch"}]

    def tags(self):
        return self._tags

    def tools(self):
        return [{"id": "o1", "name": "Slow Cooker"}]

    def create_food(self, name, plural=None, label_id=None):
        rec = {"id": f"new-{name}", "name": name, "pluralName": plural, "labelId": label_id}
        self.created_foods.append(rec)
        return rec

    def create_unit(self, name, plural=None):
        rec = {"id": f"new-{name}", "name": name, "pluralName": plural}
        self.created_units = getattr(self, "created_units", []) + [rec]
        return rec

    def create_tag(self, name):
        rec = {"id": "t-review", "name": name}
        self.created_tags.append(rec)
        self._tags.append(rec)
        return rec

    def create_organizer(self, kind, name):
        rec = {"id": f"new-{name}", "name": name}
        self.created_organizers.append((kind, name))
        if kind == "tags":
            self._tags.append(rec)
        return rec

    def group_self(self):
        return {"slug": "home"}

    def recipes_tagged(self, tag_id):
        return [r for r in self.recipes_by_slug.values()
                if any(t["id"] == tag_id for t in r.get("tags") or [])]

    # shopping
    def shopping_lists(self):
        return [{"id": "L1", "name": "Weekly shop"}]

    def shopping_list(self, list_id):
        return {"id": list_id, "listItems": self.shop_items}

    def add_recipes_to_list(self, list_id, items):
        self.added_to_list = getattr(self, "added_to_list", []) + [(list_id, copy.deepcopy(items))]
        return {"id": list_id}

    def bulk_import_urls(self, urls):
        self.bulk_imported = getattr(self, "bulk_imported", []) + [list(urls)]
        return {"reportId": "rep-1"}

    # meal planner
    def mealplans(self, start, end):
        return [e for e in getattr(self, "plan", []) if start <= e["date"] <= end]

    def household_self(self):
        return {"slug": "family"}

    def tick_shopping_items(self, items):
        ids = {i["id"] for i in items}
        for i in self.shop_items:
            if i["id"] in ids:
                i["checked"] = True
        return {"updatedItems": items}


class FakeLLM:
    """Answers each call from a script, keyed by the reply model's name."""

    def __init__(self, **script):
        self.script = script
        self.calls = []

    def structured(self, messages, reply, name, **_):
        self.calls.append(name)
        return reply.model_validate(self.script[reply.__name__])

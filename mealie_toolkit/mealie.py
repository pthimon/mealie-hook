"""Thin Mealie REST client."""

import httpx


class MealieError(RuntimeError):
    pass


class Mealie:
    def __init__(self, base_url: str, token: str, timeout: float = 120):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                 headers={"Authorization": f"Bearer {token}"})

    def req(self, method: str, path: str, body=None):
        try:
            r = self.http.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise MealieError(f"{method} {path} -> {type(e).__name__}: {e}") from None
        if r.is_error:
            raise MealieError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:400]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text.strip('"')

    def _all(self, path: str) -> list[dict]:
        sep = "&" if "?" in path else "?"
        return self.req("GET", f"{path}{sep}perPage=-1&page=1")["items"]

    # recipes
    def recipes(self) -> list[dict]:
        return self._all("/recipes")

    def recipe(self, slug: str) -> dict:
        return self.req("GET", f"/recipes/{slug}")

    def put_recipe(self, slug: str, data: dict) -> dict:
        return self.req("PUT", f"/recipes/{slug}", data)

    # vocabulary
    def foods(self) -> list[dict]:
        return self._all("/foods")

    def units(self) -> list[dict]:
        return self._all("/units")

    def labels(self) -> list[dict]:
        return self._all("/groups/labels")

    def categories(self) -> list[dict]:
        return self._all("/organizers/categories")

    def tags(self) -> list[dict]:
        return self._all("/organizers/tags")

    def tools(self) -> list[dict]:
        return self._all("/organizers/tools")

    def create_food(self, name: str, plural: str | None = None,
                    label_id: str | None = None) -> dict:
        body = {"name": name}
        if plural:
            body["pluralName"] = plural
        if label_id:
            body["labelId"] = label_id
        return self.req("POST", "/foods", body)

    def create_unit(self, name: str, plural: str | None = None) -> dict:
        body = {"name": name}
        if plural:
            body["pluralName"] = plural
        return self.req("POST", "/units", body)

    def create_tag(self, name: str) -> dict:
        return self.req("POST", "/organizers/tags", {"name": name})

    def create_organizer(self, kind: str, name: str) -> dict:
        path = {"categories": "/organizers/categories", "tags": "/organizers/tags",
                "tools": "/organizers/tools", "labels": "/groups/labels"}[kind]
        return self.req("POST", path, {"name": name})

    def group_self(self) -> dict:
        return self.req("GET", "/groups/self")

    def recipes_tagged(self, tag_id: str) -> list[dict]:
        return self._all(f"/recipes?tags={tag_id}")

    def tick_shopping_items(self, items: list[dict]) -> dict:
        """Bulk update: each item goes back whole with `checked` set, so quantities, notes,
        labels and recipe references are untouched."""
        return self.req("PUT", "/households/shopping/items",
                        [dict(i, checked=True) for i in items])

    # bulk-import reports, shopping lists, notifiers
    def report(self, report_id: str) -> dict:
        return self.req("GET", f"/groups/reports/{report_id}")

    def shopping_lists(self) -> list[dict]:
        return self._all("/households/shopping/lists")

    def shopping_list(self, list_id: str) -> dict:
        return self.req("GET", f"/households/shopping/lists/{list_id}")

    def add_recipes_to_list(self, list_id: str, items: list[dict]) -> dict:
        """[{recipeId, recipeIncrementQuantity, recipeIngredients}]: Mealie scales each
        ingredient by the increment and keeps the recipe reference, as its own dialog does."""
        return self.req("POST", f"/households/shopping/lists/{list_id}/recipe", items)

    # meal planner
    def mealplans(self, start: str, end: str) -> list[dict]:
        return self._all(f"/households/mealplans?start_date={start}&end_date={end}")

    def household_self(self) -> dict:
        return self.req("GET", "/households/self")

    def notifiers(self) -> list[dict]:
        return self._all("/households/events/notifications")

    def ensure_notifier(self, name: str, apprise_url: str) -> dict:
        """Create or update a notifier that fires on recipe_created only."""
        path = "/households/events/notifications"
        existing = next((n for n in self.notifiers() if n["name"] == name), None)
        if existing is None:
            existing = self.req("POST", path, {"name": name, "appriseUrl": apprise_url})
        options = {k: False for k in existing["options"] if k != "id"}
        options["recipeCreated"] = True
        body = {"id": existing["id"], "name": name, "appriseUrl": apprise_url, "enabled": True,
                "groupId": existing["groupId"], "householdId": existing["householdId"],
                "options": options}
        return self.req("PUT", f"{path}/{existing['id']}", body)

    def delete_notifier(self, name: str) -> bool:
        for n in self.notifiers():
            if n["name"] == name:
                self.req("DELETE", f"/households/events/notifications/{n['id']}")
                return True
        return False

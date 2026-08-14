import unittest

from main import _api_catalog, app


class APISchemaTests(unittest.TestCase):
    def test_openapi_groups_every_api_route_with_named_tags(self):
        schema = app.openapi()
        expected_paths = {
            "/api",
            "/api/health",
            "/api/gameweeks",
            "/api/scout",
            "/api/gw/scout",
            "/api/gw/playerpoints",
            "/api/fpl/gameweeks",
            "/api/fpl/teams",
            "/api/fpl/players",
            "/api/fpl/players/{player_id}",
            "/api/fpl/players/{player_id}/history",
            "/api/fpl/fixtures",
        }

        self.assertEqual(set(schema["paths"]), expected_paths)
        self.assertEqual(
            [tag["name"] for tag in schema["tags"]],
            [
                "Service",
                "Scout AI",
                "Official FPL · Gameweeks",
                "Official FPL · Teams",
                "Official FPL · Players",
                "Official FPL · Fixtures",
            ],
        )
        for methods in schema["paths"].values():
            for operation in methods.values():
                self.assertTrue(operation["tags"])

    def test_generated_catalog_reports_authentication(self):
        groups = _api_catalog()
        endpoints = [endpoint for group in groups for endpoint in group["endpoints"]]
        public_players = next(
            endpoint for endpoint in endpoints if endpoint["path"] == "/api/fpl/players"
        )
        protected_scout = next(
            endpoint for endpoint in endpoints if endpoint["path"] == "/api/gw/scout"
        )

        self.assertEqual(public_players["authentication"], "public")
        self.assertEqual(protected_scout["authentication"], "bearer")


if __name__ == "__main__":
    unittest.main()

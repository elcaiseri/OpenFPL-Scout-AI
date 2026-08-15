import unittest

from main import (
    _api_catalog,
    _documentation_config,
    _documentation_links,
    _is_production_environment,
    app,
)


class APISchemaTests(unittest.TestCase):
    def test_production_keeps_only_redoc_ui(self):
        documentation = _documentation_config(is_production=True)

        self.assertIsNone(documentation["docs_url"])
        self.assertIsNone(documentation["swagger_ui_oauth2_redirect_url"])
        self.assertEqual(documentation["redoc_url"], "/redoc")
        self.assertEqual(
            _documentation_links(is_production=True), {"redoc": "/redoc"}
        )

    def test_production_environment_detection(self):
        self.assertTrue(_is_production_environment({"OPENFPL_ENV": "production"}))
        self.assertTrue(_is_production_environment({"K_SERVICE": "openfpl"}))
        self.assertFalse(_is_production_environment({"OPENFPL_ENV": "development"}))

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
            "/api/fpl/gameweeks/status",
            "/api/fpl/gameweeks/{gameweek}/live",
            "/api/fpl/dream-team",
            "/api/fpl/gameweeks/{gameweek}/dream-team",
            "/api/fpl/fixtures/{fixture_id}/stats",
            "/api/fpl/managers/{entry_id}",
            "/api/fpl/managers/{entry_id}/history",
            "/api/fpl/managers/{entry_id}/transfers",
            "/api/fpl/managers/{entry_id}/gameweeks/{gameweek}/picks",
            "/api/fpl/leagues/classic/{league_id}/standings",
            "/api/fpl/leagues/h2h/{league_id}/standings",
            "/api/fpl/leagues/h2h/{league_id}/matches",
            "/api/fpl/leagues/{league_id}/cup-status",
            "/api/fpl/regions",
            "/api/fpl/set-piece-notes",
            "/api/fpl/rankings/best-private-leagues",
            "/api/fpl/rankings/most-valuable-teams",
            "/api/fpl/gameweeks/{gameweek}/winners",
            "/api/fpl/phases/{phase_id}/winners",
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
                "Official FPL · Managers",
                "Official FPL · Leagues & Cups",
                "Official FPL · Reference & Rankings",
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

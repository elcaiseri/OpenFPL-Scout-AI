import asyncio
import unittest
from unittest.mock import patch

import pandas as pd

from main import (
    RAPIDAPI_PRICING_URL,
    _api_catalog,
    _documentation_config,
    _documentation_links,
    _is_production_environment,
    app,
    rate_public_manager_team,
    redirect_to_rapidapi,
)


class FakeRatingOfficialClient:
    def mapped_manager(self, entry_id):
        return {
            "id": entry_id,
            "name": "Test XI",
            "player_first_name": "Test",
            "player_last_name": "Manager",
            "current_event": 1,
        }

    def mapped_manager_picks(self, entry_id, gameweek):
        positions = [1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 1, 2, 3, 4]
        return {
            "entry_id": entry_id,
            "gameweek": gameweek,
            "picks": [
                {
                    "element": index,
                    "position": index,
                    "multiplier": 2 if index == 1 else 1 if index <= 11 else 0,
                    "is_captain": index == 1,
                    "is_vice_captain": index == 2,
                    "player": {
                        "id": index,
                        "position": position,
                        "price": 5.0,
                    },
                }
                for index, position in enumerate(positions, start=1)
            ],
        }


class FakeRatingScout:
    def __init__(self):
        self.official_client = FakeRatingOfficialClient()

    def get_official_predictions(self, gameweek):
        positions = [1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 1, 2, 3, 4]
        predictions = pd.DataFrame(
            [
                {
                    "id": index,
                    "web_name": f"Player {index}",
                    "team_name": f"Club {index % 6}",
                    "element_type": position,
                    "expected_points": 5.0,
                    "availability_factor": 1.0,
                    "selected_by_percent": 20.0,
                }
                for index, position in enumerate(positions, start=1)
            ]
        )
        predictions.attrs["gameweek"] = gameweek
        predictions.attrs["inference"] = {"strategy": "model-ensemble"}
        predictions.attrs["source"] = "official-fpl"
        return predictions

    def select_optimal_team(self, predictions):
        return predictions.copy()


class APISchemaTests(unittest.TestCase):
    def test_rapidapi_redirect_tracks_placement_without_caching(self):
        with patch("main.logger.info") as log_info:
            response = asyncio.run(redirect_to_rapidapi(placement="topbar"))

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], RAPIDAPI_PRICING_URL)
        self.assertEqual(response.headers["cache-control"], "no-store")
        log_info.assert_called_once_with(
            "outbound_click destination=rapidapi placement=%s",
            "topbar",
        )

    def test_public_team_rating_combines_manager_picks_and_predictions(self):
        with patch("main.scout", FakeRatingScout(), create=True):
            result = asyncio.run(rate_public_manager_team(entry_id=123, gameweek=1))

        self.assertEqual(result.entry_id, 123)
        self.assertEqual(result.team_name, "Test XI")
        self.assertEqual(result.rating, 100)
        self.assertEqual(result.grade, "A+")
        self.assertEqual(len(result.squad), 15)

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
            "/api/scout/team-rating",
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

    def test_only_ui_api_routes_are_public(self):
        groups = _api_catalog()
        endpoints = [endpoint for group in groups for endpoint in group["endpoints"]]
        public_routes = {
            (method, endpoint["path"])
            for endpoint in endpoints
            if endpoint["authentication"] == "public"
            for method in endpoint["methods"]
        }

        self.assertEqual(
            public_routes,
            {
                ("GET", "/api"),
                ("GET", "/api/fpl/gameweeks"),
                ("GET", "/api/fpl/players"),
                ("GET", "/api/fpl/fixtures"),
                ("GET", "/api/fpl/gameweeks/status"),
                ("GET", "/api/scout"),
                ("GET", "/api/scout/team-rating"),
            },
        )
        self.assertTrue(
            all(
                endpoint["authentication"] == "bearer"
                for endpoint in endpoints
                if not all(
                    (method, endpoint["path"]) in public_routes
                    for method in endpoint["methods"]
                )
            )
        )


if __name__ == "__main__":
    unittest.main()

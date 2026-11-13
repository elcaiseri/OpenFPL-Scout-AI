import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException

from main import (
    _api_catalog,
    _documentation_config,
    _documentation_links,
    _is_production_environment,
    app,
    get_player_predictions,
    rate_public_manager_team,
    generate_public_scout_team,
    refresh_public_scout_predictions,
    sync_public_scout_results,
)
from src.models import PlayerPointsModel
from src.official_fpl import OfficialFPLAPIError


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

    def get_official_predictions(
        self, gameweek, *, collect_results=True, allow_stale=False
    ):
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


class DepartedPlayerRatingScout(FakeRatingScout):
    """Player 5 is no longer selectable, so the model has no projection."""

    def get_official_predictions(
        self, gameweek, *, collect_results=True, allow_stale=False
    ):
        predictions = super().get_official_predictions(
            gameweek, collect_results=collect_results
        )
        trimmed = predictions.loc[predictions["id"] != 5].reset_index(drop=True)
        trimmed.attrs = predictions.attrs
        return trimmed


class TransferScout(FakeRatingScout):
    """Player 16, a forward not in the squad, projects 9 xPts for 5.5m.

    ``official`` overrides player 16's current official details, and
    ``clubs`` moves squad players to other clubs, by player id.
    """

    def __init__(
        self, frozen=False, deadline="2099-01-01T10:00:00Z", official=None, clubs=None
    ):
        super().__init__()
        self.frozen = frozen
        self.clubs = clubs or {}
        client = self.official_client
        manager = client.mapped_manager
        client.mapped_manager = lambda entry_id: {
            **manager(entry_id),
            "last_deadline_bank": 0.5,
        }
        client.mapped_players = lambda: [
            {
                "id": index,
                "price": 5.5 if index == 16 else 5.0,
                "status": "a",
                "can_select": True,
                **(official if index == 16 and official else {}),
            }
            for index in range(1, 17)
        ]
        client.bootstrap = lambda: {"events": [{"id": 1, "deadline_time": deadline}]}

    def get_official_predictions(self, gameweek, *, collect_results=True):
        predictions = super().get_official_predictions(
            gameweek, collect_results=collect_results
        )
        extra = pd.DataFrame(
            [
                {
                    "id": 16,
                    "web_name": "Player 16",
                    "team_name": "Club 5",
                    "element_type": 4,
                    "expected_points": 9.0,
                    "availability_factor": 1.0,
                    "selected_by_percent": 2.0,
                }
            ]
        )
        combined = pd.concat([predictions, extra], ignore_index=True)
        for player_id, club in self.clubs.items():
            combined.loc[combined["id"] == player_id, "team_name"] = club
        combined.attrs = {**predictions.attrs, "frozen": self.frozen}
        return combined


class VenueScout(FakeRatingScout):
    """Player 1 plays at home, 2 away, 3 once at each venue, and 4 blanks."""

    def __init__(self):
        super().__init__()
        self.data_archive = SimpleNamespace(capture_squad=lambda *args: None)

    def get_official_predictions(
        self, gameweek, *, collect_results=True, allow_stale=False
    ):
        predictions = super().get_official_predictions(
            gameweek, collect_results=collect_results
        )
        predictions["was_home"] = pd.Series(
            [True, False, None, None, *[True] * 11], dtype=object
        )
        return predictions


class APISchemaTests(unittest.TestCase):
    def test_public_squad_does_not_wait_for_results_collection(self):
        scout = VenueScout()
        with patch("main.scout", scout, create=True), patch.object(
            scout, "get_official_predictions", wraps=scout.get_official_predictions
        ) as predictions:
            result = asyncio.run(generate_public_scout_team(gameweek=1))

        predictions.assert_called_once_with(1, collect_results=False, allow_stale=True)
        self.assertEqual(len(result.scout_team), 15)
        self.assertFalse(result.refresh_due)

    def test_a_stale_public_squad_asks_the_page_to_refresh(self):
        scout = VenueScout()
        predictions = scout.get_official_predictions
        scout.get_official_predictions = lambda *args, **kwargs: (
            lambda frame: (frame.attrs.update(refresh_due=True), frame)[1]
        )(predictions(*args, **kwargs))
        with patch("main.scout", scout, create=True):
            result = asyncio.run(generate_public_scout_team(gameweek=1))

        self.assertTrue(result.refresh_due)

    def test_refresh_predicts_inside_its_own_request(self):
        scout = SimpleNamespace(refresh_predictions=lambda gameweek: f"refreshed {gameweek}")
        with patch("main.scout", scout, create=True):
            result = asyncio.run(refresh_public_scout_predictions(gameweek=11))

        self.assertEqual(result, {"status": "refreshed 11"})

    def test_a_failed_refresh_reports_the_upstream_error(self):
        def unavailable(gameweek):
            raise OfficialFPLAPIError("FPL is updating")

        scout = SimpleNamespace(refresh_predictions=unavailable)
        with patch("main.scout", scout, create=True):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(refresh_public_scout_predictions(gameweek=11))

        self.assertEqual(raised.exception.status_code, 502)

    def test_public_squad_carries_current_official_details(self):
        scout = VenueScout()
        scout.official_client.mapped_players = lambda: [
            {
                "id": 1,
                "price": 7.5,
                "status": "d",
                "news": "Knock",
                "selected_by_percent": 1.0,
                "code": 99,
            },
        ]
        with patch("main.scout", scout, create=True):
            result = asyncio.run(generate_public_scout_team(gameweek=1))

        first = next(player for player in result.scout_team if player["id"] == 1)
        self.assertEqual(first["price"], 7.5)
        self.assertEqual(first["status"], "d")
        self.assertEqual(first["news"], "Knock")
        # Only the fields the app shows are added, and predictions win.
        self.assertNotIn("code", first)
        self.assertEqual(first["selected_by_percent"], 20.0)

    def test_public_squad_is_served_when_official_details_fail(self):
        scout = VenueScout()

        def unavailable():
            raise OfficialFPLAPIError("FPL is updating")

        scout.official_client.mapped_players = unavailable
        with patch("main.scout", scout, create=True):
            result = asyncio.run(generate_public_scout_team(gameweek=1))

        self.assertEqual(len(result.scout_team), 15)
        self.assertNotIn("status", result.scout_team[0])

    def test_results_sync_collects_inside_its_own_public_request(self):
        scout = SimpleNamespace(official_client=object(), data_archive=object())
        with patch("main.scout", scout, create=True), patch.object(
            scout, "data_archive"
        ) as archive:
            archive.collect_results.return_value = {"status": "throttled"}
            result = asyncio.run(sync_public_scout_results())

        archive.collect_results.assert_called_once_with(scout.official_client)
        self.assertEqual(result, {"status": "throttled"})

    def test_venue_filter_skips_players_without_a_single_venue(self):
        with patch("main.scout", VenueScout(), create=True):
            home, away = (
                asyncio.run(
                    get_player_predictions(
                        PlayerPointsModel(gameweek=1, was_home=venue), api_key="key"
                    )
                )
                for venue in (True, False)
            )

        home_ids = [player["id"] for player in home.player_points]
        self.assertEqual([player["id"] for player in away.player_points], [2])
        self.assertNotIn(3, home_ids)
        self.assertNotIn(4, home_ids)
        self.assertIn(1, home_ids)

    def test_public_team_rating_combines_manager_picks_and_predictions(self):
        with patch("main.scout", FakeRatingScout(), create=True):
            result = asyncio.run(rate_public_manager_team(entry_id=123, gameweek=1))

        self.assertEqual(result.entry_id, 123)
        self.assertEqual(result.team_name, "Test XI")
        self.assertEqual(result.rating, 100)
        self.assertEqual(result.grade, "A+")
        self.assertEqual(len(result.squad), 15)

    def test_team_rating_suggests_affordable_transfers_before_the_deadline(self):
        with patch("main.scout", TransferScout(), create=True):
            result = asyncio.run(rate_public_manager_team(entry_id=123, gameweek=1))

        self.assertEqual(result.bank, 0.5)
        self.assertTrue(result.transfers_open)
        self.assertEqual(result.clubs_over_limit, {})
        self.assertEqual(len(result.transfers), 1)
        [suggestion] = result.transfers
        self.assertEqual(suggestion["in"]["id"], 16)
        self.assertEqual(suggestion["out"]["position"], 4)
        self.assertEqual(suggestion["cost"], 0.5)
        self.assertGreater(suggestion["gain"], 0)

    def test_a_frozen_gameweek_suggests_no_transfers(self):
        with patch("main.scout", TransferScout(frozen=True), create=True):
            result = asyncio.run(rate_public_manager_team(entry_id=123, gameweek=1))

        self.assertTrue(result.frozen)
        self.assertFalse(result.transfers_open)
        self.assertEqual(result.transfers, [])

    def test_a_closed_gameweek_suggests_no_transfers_even_when_not_frozen(self):
        # With the archive off, or a freeze that failed, a gameweek past its
        # deadline is not frozen; the deadline itself closes transfers.
        scout = TransferScout(deadline="2000-01-01T10:00:00Z")
        with patch("main.scout", scout, create=True):
            result = asyncio.run(rate_public_manager_team(entry_id=123, gameweek=1))

        self.assertFalse(result.frozen)
        self.assertFalse(result.transfers_open)
        self.assertEqual(result.transfers, [])

    def test_a_player_now_unavailable_is_not_suggested_despite_the_forecast(self):
        # The forecast still gives player 16 availability_factor 1.0.
        for official in (
            {"can_select": False},
            {"status": "i"},
            {"status": "s"},
            {"chance_of_playing_next_round": 0},
        ):
            with self.subTest(official=official):
                with patch("main.scout", TransferScout(official=official), create=True):
                    result = asyncio.run(
                        rate_public_manager_team(entry_id=123, gameweek=1)
                    )

                self.assertTrue(result.transfers_open)
                self.assertEqual(result.transfers, [])

    def test_a_squad_over_the_club_limit_is_flagged_and_not_offered_other_sales(self):
        # Players 1, 7 and 13 are Club 1; moving player 4 there makes four.
        scout = TransferScout(clubs={4: "Club 1"})
        with patch("main.scout", scout, create=True):
            result = asyncio.run(rate_public_manager_team(entry_id=123, gameweek=1))

        self.assertEqual(result.clubs_over_limit, {"Club 1": 4})
        # Player 16 is a forward and no Club 1 player is, so no transfer
        # FPL would accept raises the projection.
        self.assertEqual(result.transfers, [])

    def test_team_rating_scores_squads_holding_unprojectable_players(self):
        with patch("main.scout", DepartedPlayerRatingScout(), create=True):
            result = asyncio.run(rate_public_manager_team(entry_id=123, gameweek=1))

        departed = next(player for player in result.squad if player["id"] == 5)
        self.assertEqual(len(result.squad), 15)
        self.assertTrue(departed["projection_missing"])
        self.assertEqual(departed["expected_points"], 0.0)
        self.assertLess(result.rating, 100)
        self.assertTrue(
            any("Availability needs checking" in risk for risk in result.risks)
        )

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
            "/api/scout/sync-results",
            "/api/scout/refresh",
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
                ("POST", "/api/scout/sync-results"),
                ("POST", "/api/scout/refresh"),
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

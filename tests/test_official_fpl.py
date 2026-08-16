import unittest
from urllib.parse import urlparse

import numpy as np
import requests

from src.official_fpl import (
    OfficialFPLAPIError,
    OfficialFPLClient,
    OfficialFPLNotFoundError,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}", response=self
            )

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, timeout):
        parsed = urlparse(url)
        path = parsed.path.removeprefix("/api/")
        if parsed.query:
            path = f"{path}?{parsed.query}"
        self.calls.append((path, timeout))
        return FakeResponse(self.responses[path])


def bootstrap(finished=False):
    return {
        "events": [
            {
                "id": 1,
                "name": "Gameweek 1",
                "finished": finished,
                "is_current": False,
                "is_next": not finished,
            },
            {
                "id": 2,
                "name": "Gameweek 2",
                "finished": False,
                "is_current": False,
                "is_next": finished,
            },
        ],
        "teams": [
            {"id": 1, "name": "Arsenal"},
            {"id": 2, "name": "Man Utd"},
        ],
        "element_types": [{"id": 3, "singular_name": "Midfielder"}],
        "elements": [
            {
                "id": 10,
                "element_type": 3,
                "web_name": "Player",
                "team": 2,
                "can_select": True,
                "now_cost": 75,
                "selected_by_percent": "12.5",
                "total_points": 0,
                "minutes": 0,
                "goals_scored": 0,
                "assists": 0,
                "clean_sheets": 0,
                "goals_conceded": 0,
                "expected_goals": "0.00",
                "expected_assists": "0.00",
                "expected_goal_involvements": "0.00",
                "expected_goals_conceded": "0.00",
            }
        ],
    }


def add_player(payload, player_id=11, web_name="New Player"):
    payload["elements"].append(
        payload["elements"][0]
        | {
            "id": player_id,
            "web_name": web_name,
            "team": 1,
        }
    )
    return payload


class OfficialFPLClientTests(unittest.TestCase):
    def test_preseason_uses_official_bootstrap_baseline(self):
        session = FakeSession({"bootstrap-static/": bootstrap()})
        client = OfficialFPLClient(session=session)

        result = client.player_history(gameweek=1)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.loc[0, "team_name"], "Manchester United")
        self.assertEqual(result.loc[0, "now_cost"], 7.5)
        self.assertEqual(result.loc[0, "selected_by_percent"], 12.5)
        self.assertTrue(result.loc[0, "can_select"])
        self.assertTrue(np.isnan(result.loc[0, "chance_of_playing_next_round"]))
        self.assertEqual(result.loc[0, "gameweek"], 0)
        self.assertTrue(np.isnan(result.loc[0, "opponent_team_name"]))
        self.assertEqual([call[0] for call in session.calls], ["bootstrap-static/"])

    def test_maps_official_player_history_to_model_schema(self):
        history = {
            "history": [
                {
                    "round": 1,
                    "opponent_team": 1,
                    "was_home": False,
                    "value": 76,
                    "total_points": 8,
                    "minutes": 90,
                    "goals_scored": 1,
                    "assists": 0,
                    "clean_sheets": 1,
                    "goals_conceded": 0,
                    "expected_goals": "0.71",
                    "expected_assists": "0.11",
                    "expected_goal_involvements": "0.82",
                    "expected_goals_conceded": "0.44",
                    "clearances_blocks_interceptions": 2,
                    "recoveries": 5,
                    "tackles": 1,
                    "defensive_contribution": 3,
                }
            ],
            "fixtures": [],
            "history_past": [],
        }
        session = FakeSession(
            {
                "bootstrap-static/": bootstrap(finished=True),
                "element-summary/10/": history,
            }
        )
        client = OfficialFPLClient(session=session, max_workers=1)

        result = client.player_history(gameweek=2)

        self.assertEqual(result.loc[0, "opponent_team_name"], "Arsenal")
        self.assertFalse(result.loc[0, "was_home"])
        self.assertEqual(result.loc[0, "goals"], 1.0)
        self.assertEqual(result.loc[0, "expected_goals"], 0.71)
        self.assertEqual(result.loc[0, "clean_sheet"], 1.0)

    def test_retains_players_without_prior_match_history(self):
        payload = add_player(bootstrap(finished=True))
        session = FakeSession(
            {
                "bootstrap-static/": payload,
                "element-summary/10/": {
                    "history": [
                        {
                            "round": 1,
                            "opponent_team": 1,
                            "was_home": False,
                            "value": 75,
                            "total_points": 2,
                        }
                    ]
                },
                "element-summary/11/": {"history": []},
            }
        )
        client = OfficialFPLClient(session=session, max_workers=1)

        result = client.player_history(gameweek=2)

        self.assertEqual(set(result["id"]), {10, 11})
        new_player = result.loc[result["id"] == 11].iloc[0]
        self.assertEqual(new_player["web_name"], "New Player")
        self.assertTrue(np.isnan(new_player["total_points"]))

    def test_maps_official_double_gameweek_fixtures(self):
        fixtures = [
            {
                "id": 1,
                "event": 2,
                "team_h": 1,
                "team_a": 2,
                "kickoff_time": "2026-09-01T12:00:00Z",
                "finished": False,
                "team_h_difficulty": 2,
                "team_a_difficulty": 4,
            },
            {
                "id": 2,
                "event": 2,
                "team_h": 2,
                "team_a": 1,
                "kickoff_time": "2026-09-04T12:00:00Z",
                "finished": False,
                "team_h_difficulty": 3,
                "team_a_difficulty": 3,
            },
        ]
        session = FakeSession({"bootstrap-static/": bootstrap(), "fixtures/": fixtures})
        client = OfficialFPLClient(session=session)

        result = client.fixtures_for_gameweek(2)

        self.assertEqual(result["Arsenal"]["fixture_count"], 2)
        self.assertEqual(
            result["Manchester United"]["opponent_team_name"],
            "Arsenal / Arsenal",
        )
        self.assertEqual(len(result["Arsenal"]["fixtures"]), 2)
        self.assertEqual(result["Arsenal"]["difficulty"], 2.5)

    def test_available_gameweeks_follow_official_event_state(self):
        session = FakeSession({"bootstrap-static/": bootstrap(finished=True)})
        client = OfficialFPLClient(session=session)

        result = client.available_gameweeks()

        self.assertEqual(result["gameweeks"], [1, 2])
        self.assertEqual(result["latest"], 2)
        self.assertEqual(result["source"], "official-fpl")

    def test_maps_public_player_and_team_resources(self):
        session = FakeSession({"bootstrap-static/": bootstrap()})
        client = OfficialFPLClient(session=session)

        teams = client.mapped_teams()
        players = client.mapped_players(team_id=2, element_type=3)

        self.assertEqual(teams[1]["name"], "Manchester United")
        self.assertEqual(players[0]["position"], "Midfielder")
        self.assertEqual(players[0]["price"], 7.5)
        self.assertEqual(players[0]["selected_by_percent"], 12.5)
        self.assertIsNone(players[0]["recoveries"])

    def test_maps_public_fixtures_with_named_teams(self):
        fixtures = [
            {
                "id": 20,
                "event": 1,
                "team_h": 1,
                "team_a": 2,
                "team_h_score": None,
                "team_a_score": None,
                "team_h_difficulty": 2,
                "team_a_difficulty": 4,
                "kickoff_time": "2026-08-21T19:00:00Z",
                "started": False,
                "finished": False,
            }
        ]
        client = OfficialFPLClient(
            session=FakeSession(
                {"bootstrap-static/": bootstrap(), "fixtures/": fixtures}
            )
        )

        result = client.mapped_fixtures(gameweek=1, team_id=2)

        self.assertEqual(result[0]["home_team"]["name"], "Arsenal")
        self.assertEqual(result[0]["away_team"]["name"], "Manchester United")
        self.assertEqual(result[0]["away_team"]["difficulty"], 4)

    def test_maps_public_player_summary(self):
        summary = {
            "history": [
                {
                    "round": 1,
                    "fixture": 20,
                    "opponent_team": 1,
                    "was_home": False,
                    "value": 76,
                    "total_points": 8,
                    "minutes": 90,
                }
            ],
            "fixtures": [
                {
                    "id": 30,
                    "event": 2,
                    "team_h": 2,
                    "team_a": 1,
                    "is_home": True,
                    "difficulty": 2,
                }
            ],
            "history_past": [
                {
                    "season_name": "2025/26",
                    "start_cost": 70,
                    "end_cost": 75,
                    "total_points": 120,
                }
            ],
        }
        client = OfficialFPLClient(
            session=FakeSession(
                {
                    "bootstrap-static/": bootstrap(),
                    "element-summary/10/": summary,
                }
            )
        )

        result = client.mapped_player_summary(10)

        self.assertEqual(result["history"][0]["opponent_team_name"], "Arsenal")
        self.assertEqual(result["history"][0]["price"], 7.6)
        self.assertEqual(
            result["upcoming_fixtures"][0]["opponent_team_name"], "Arsenal"
        )
        self.assertEqual(result["past_seasons"][0]["start_price"], 7.0)

    def test_public_player_mapping_raises_not_found(self):
        client = OfficialFPLClient(
            session=FakeSession({"bootstrap-static/": bootstrap()})
        )

        with self.assertRaises(OfficialFPLNotFoundError):
            client.mapped_player(999)

    def test_maps_live_gameweek_with_player_identity(self):
        live = {
            "elements": [
                {
                    "id": 10,
                    "stats": {"total_points": 6},
                    "explain": [],
                    "modified": False,
                }
            ]
        }
        client = OfficialFPLClient(
            session=FakeSession(
                {"bootstrap-static/": bootstrap(), "event/1/live/": live}
            )
        )

        result = client.mapped_event_live(1)

        self.assertEqual(result["gameweek"], 1)
        self.assertEqual(result["elements"][0]["web_name"], "Player")
        self.assertEqual(result["elements"][0]["team_name"], "Manchester United")
        self.assertEqual(result["elements"][0]["stats"]["total_points"], 6)

    def test_maps_manager_profile_and_transfers(self):
        manager = {
            "id": 123,
            "name": "Test XI",
            "favourite_team": 2,
            "last_deadline_value": 1015,
            "last_deadline_bank": 7,
        }
        transfers = [
            {
                "event": 2,
                "element_in": 10,
                "element_out": 11,
                "element_in_cost": 75,
                "element_out_cost": 55,
            }
        ]
        payload = add_player(bootstrap(), player_id=11, web_name="Outgoing")
        client = OfficialFPLClient(
            session=FakeSession(
                {
                    "bootstrap-static/": payload,
                    "entry/123/": manager,
                    "entry/123/transfers/": transfers,
                }
            )
        )

        profile = client.mapped_manager(123)
        mapped_transfers = client.mapped_manager_transfers(123)

        self.assertEqual(profile["favourite_team"]["name"], "Manchester United")
        self.assertEqual(profile["last_deadline_value"], 101.5)
        self.assertEqual(mapped_transfers[0]["player_in"]["web_name"], "Player")
        self.assertEqual(mapped_transfers[0]["element_out_cost"], 5.5)

    def test_classic_standings_preserve_full_pagination_options(self):
        path = (
            "leagues-classic/314/standings/"
            "?page_new_entries=3&page_standings=2&phase=4"
        )
        session = FakeSession({path: {"league": {"id": 314}, "standings": {}}})
        client = OfficialFPLClient(session=session)

        result = client.classic_league_standings(
            314, page_standings=2, page_new_entries=3, phase=4
        )

        self.assertEqual(result["league"]["id"], 314)
        self.assertEqual(session.calls[0][0], path)

    def test_upstream_404_becomes_typed_not_found(self):
        client = OfficialFPLClient(
            session=FakeSession({"regions/": []})
        )
        client.session.responses["regions/"] = []
        client.session.get = lambda url, timeout: FakeResponse({}, status_code=404)

        with self.assertRaises(OfficialFPLNotFoundError):
            client.regions()

    def test_rejects_invalid_bootstrap_schema(self):
        client = OfficialFPLClient(
            session=FakeSession({"bootstrap-static/": {"elements": []}})
        )

        with self.assertRaises(OfficialFPLAPIError):
            client.bootstrap()


if __name__ == "__main__":
    unittest.main()

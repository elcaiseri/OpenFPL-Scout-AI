"""A gameweek's prediction must not change once its deadline has passed."""

import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from main import _generate_scout_response
from src.data_archive import DataArchive
from src.features import MODEL_FEATURES
from src.official_fpl import OfficialFPLClient
from src.scout import FPLScout

BEFORE_GW3_DEADLINE = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
AFTER_GW3_DEADLINE = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
DEADLINES = {
    1: "2026-08-15T10:00:00Z",
    2: "2026-08-22T10:00:00Z",
    3: "2026-08-29T10:00:00Z",
    4: "2026-09-05T10:00:00Z",
}
PAIRS = {
    1: [(1, 2), (3, 4), (5, 6)],
    2: [(2, 3), (4, 5), (6, 1)],
    3: [(1, 3), (2, 5), (4, 6)],
    4: [(3, 2), (5, 1), (6, 4)],
}


class FakeFPL:
    """Official FPL for six clubs, where GW1-2 are finished."""

    def __init__(self):
        self.current = 2
        self.finished_through = 2
        self.elements = []
        for team in range(1, 7):
            for position in [1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4]:
                player_id = len(self.elements) + 1
                self.elements.append(
                    {
                        "id": player_id,
                        "web_name": f"P{player_id}",
                        "team": team,
                        "element_type": position,
                        "status": "a",
                        "can_select": True,
                        "chance_of_playing_next_round": None,
                        "now_cost": 50,
                        "selected_by_percent": "5.0",
                    }
                )
        self.fixtures = [
            {
                "id": gameweek * 10 + index,
                "event": gameweek,
                "team_h": home,
                "team_a": away,
                "kickoff_time": None,
                "finished": gameweek <= 2,
                "started": gameweek <= 2,
            }
            for gameweek, pairs in PAIRS.items()
            for index, (home, away) in enumerate(pairs)
        ]

    def events(self):
        return [
            {
                "id": gameweek,
                "deadline_time": deadline,
                "finished": gameweek <= self.finished_through,
                "data_checked": gameweek <= self.finished_through,
                "is_current": gameweek == self.current,
                "is_next": gameweek == self.current + 1,
            }
            for gameweek, deadline in DEADLINES.items()
        ]

    def summary(self, player_id):
        player = self.elements[player_id - 1]
        rows = []
        for fixture in self.fixtures:
            if fixture["finished"] and player["team"] in (
                fixture["team_h"],
                fixture["team_a"],
            ):
                home = fixture["team_h"] == player["team"]
                opponent = fixture["team_a"] if home else fixture["team_h"]
                rows.append(
                    {
                        "round": fixture["event"],
                        "fixture": fixture["id"],
                        "was_home": home,
                        "opponent_team": opponent,
                        "minutes": 90,
                        "goals_scored": player_id % 4,
                        "total_points": 2,
                        "value": 50,
                    }
                )
        return {"history": rows, "fixtures": [], "history_past": []}

    def get(self, url, timeout):
        path = url.split("/api/", 1)[1]
        if path == "bootstrap-static/":
            payload = {
                "elements": self.elements,
                "teams": [{"id": team, "name": f"Club {team}"} for team in range(1, 7)],
                "events": self.events(),
                "element_types": [{"id": position} for position in range(1, 5)],
            }
        elif path == "fixtures/":
            payload = self.fixtures
        elif path.startswith("element-summary/"):
            payload = self.summary(int(path.split("/")[1]))
        else:
            payload = {"elements": []}
        return FakeResponse(json.loads(json.dumps(payload)))

    def after_the_deadline(self, injured_player):
        """GW3 goes live: an injury, an ownership swing, and a postponement."""
        self.current = 3
        self.elements[injured_player - 1].update(
            status="i", chance_of_playing_next_round=0
        )
        self.elements[0]["selected_by_percent"] = "38.0"
        postponed = next(fixture for fixture in self.fixtures if fixture["id"] == 32)
        postponed["event"] = None

    def finish(self, gameweek):
        """Every fixture of the gameweek is played and the data is checked."""
        self.finished_through = gameweek
        for fixture in self.fixtures:
            if fixture["event"] == gameweek:
                fixture.update(started=True, finished=True)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class GoalsModel:
    feature_names_in_ = np.asarray(MODEL_FEATURES)

    def predict(self, features):
        return 2.0 + 3.0 * features["goals"].fillna(0).to_numpy()


def records(frame):
    """What an API response contains for this frame."""
    return json.loads(frame.to_json(orient="records"))


class FrozenForecastTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = [BEFORE_GW3_DEADLINE]
        self.monotonic = [0.0]
        self.fpl = FakeFPL()
        self.client = OfficialFPLClient(session=self.fpl)
        self.archive = DataArchive(
            Path(self.directory.name),
            configured_season="2026-2027",
            clock=lambda: self.now[0],
        )
        self.scout = FPLScout(
            {
                "inference": {"minimum_successful_models": 1},
                "fpl_data_inference": {"enabled": False},
                "models": {"model": {"path": "model.pkl"}},
            },
            model_loader=lambda path: GoalsModel(),
            official_client=self.client,
            data_archive=self.archive,
        )
        self.scout.clock = lambda: self.monotonic[0]
        # Frozen reads collect results in the background; let that finish
        # before the archive directory is removed.
        self.addCleanup(self.archive.wait_for_results, 30)

    def request(self, gameweek=3):
        """One scout request, as the API makes it."""
        predictions = self.scout.get_official_predictions(gameweek)
        squad = self.scout.select_optimal_team(predictions)
        self.archive.capture_squad(predictions, squad)
        return predictions, squad

    def pass_the_deadline(self, injured_player):
        self.now[0] = AFTER_GW3_DEADLINE
        self.fpl.after_the_deadline(injured_player)
        self.client.clear_cache()
        self.monotonic[0] += 3600

    def captain_id(self, squad):
        return int(squad.loc[squad["role"] == "captain", "id"].iloc[0])

    def test_recalling_a_closed_gameweek_returns_the_pre_deadline_forecast(self):
        before, squad_before = self.request()
        self.pass_the_deadline(injured_player=self.captain_id(squad_before))

        after, squad_after = self.request()

        self.assertFalse(before.attrs["frozen"])
        self.assertTrue(after.attrs["frozen"])
        self.assertIsNotNone(after.attrs["forecast_captured_at"])
        self.assertEqual(records(after), records(before))
        self.assertEqual(records(squad_after), records(squad_before))
        self.assertEqual(list(squad_after.columns), list(squad_before.columns))
        self.assertEqual(after.attrs["inference"]["strategy"], "model-ensemble")

    def test_the_same_changes_do_alter_a_gameweek_that_is_still_open(self):
        before, squad_before = self.request(gameweek=4)
        self.pass_the_deadline(injured_player=self.captain_id(squad_before))

        after, _ = self.request(gameweek=4)

        injured = self.captain_id(squad_before)
        self.assertFalse(after.attrs["frozen"])
        self.assertEqual(after.set_index("id").loc[injured, "expected_points"], 0.0)
        self.assertNotEqual(records(after), records(before))

    def test_closed_gameweek_without_an_archived_forecast_is_predicted_live(self):
        self.now[0] = AFTER_GW3_DEADLINE

        predictions, _ = self.request()

        self.assertFalse(predictions.attrs["frozen"])

    def test_squad_from_other_predictions_is_reselected_from_the_frozen_ones(self):
        _, squad_before = self.request()
        squad_file = Path(self.directory.name) / "2026-2027/squads/gw_03.json"
        payload = json.loads(squad_file.read_text(encoding="utf-8"))
        payload["predictions_sha256"] = "another-instance"
        payload["players"] = payload["players"][:1]
        squad_file.write_text(json.dumps(payload), encoding="utf-8")
        self.pass_the_deadline(injured_player=self.captain_id(squad_before))

        after, squad_after = self.request()

        self.assertTrue(after.attrs["frozen"])
        self.assertIsNone(after.attrs["frozen_squad"])
        self.assertEqual(len(squad_after), 15)
        self.assertEqual(
            sorted(squad_after["id"].tolist()), sorted(squad_before["id"].tolist())
        )

    def test_a_forecast_an_older_version_archived_after_the_deadline_is_not_frozen(
        self,
    ):
        self.now[0] = AFTER_GW3_DEADLINE
        live, _ = self.request()
        # The previous version archived every request, even after the deadline.
        root = Path(self.directory.name) / "2026-2027"
        (root / "predictions").mkdir(parents=True, exist_ok=True)
        (root / "metadata").mkdir(parents=True, exist_ok=True)
        legacy = live.copy()
        legacy["expected_points"] = 99.0
        legacy.to_csv(root / "predictions/gw_03.csv", index=False)
        (root / "metadata/gw_03.json").write_text(
            json.dumps(
                {
                    "archive_schema_version": 1,
                    "captured_at_utc": "2026-08-30T11:00:00+00:00",
                    "prediction_gameweek": 3,
                    "source": "official-fpl",
                }
            ),
            encoding="utf-8",
        )
        self.monotonic[0] += 3600

        again, _ = self.request()

        self.assertFalse(again.attrs["frozen"])
        self.assertEqual(records(again), records(live))

    def test_serving_the_final_frozen_forecast_still_archives_its_results(self):
        self.now[0] = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        self.fpl.current = 3
        self.request(gameweek=4)
        # GW4 is the season's last gameweek: once its deadline passes, the
        # default request keeps resolving to it and is always served frozen.
        self.now[0] = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
        self.fpl.current = 4
        self.fpl.finish(4)
        self.client.clear_cache()
        self.monotonic[0] += 3600

        release = threading.Event()
        fetch_history = self.client.player_history

        def slow_history(*args, **kwargs):
            release.wait(5)
            return fetch_history(*args, **kwargs)

        root = Path(self.directory.name) / "2026-2027/official"
        with patch.object(self.client, "player_history", slow_history):
            predictions = self.scout.get_official_predictions()
            # The frozen forecast is served without waiting for collection.
            collected_before_returning = (root / "history/before_gw_05.csv").exists()
            release.set()
            self.archive.wait_for_results(10)

        self.assertEqual(predictions.attrs["gameweek"], 4)
        self.assertTrue(predictions.attrs["frozen"])
        self.assertFalse(collected_before_returning)
        self.assertTrue((root / "player-stats/gw_04.csv").is_file())
        self.assertTrue((root / "live/gw_04.json").is_file())
        self.assertTrue((root / "history/before_gw_05.csv").is_file())

    def test_api_response_marks_the_frozen_forecast(self):
        before = asyncio.run(self.respond())
        _, squad_before = self.request()
        self.pass_the_deadline(injured_player=self.captain_id(squad_before))

        after = asyncio.run(self.respond())

        self.assertFalse(before.frozen)
        self.assertTrue(after.frozen)
        self.assertIsNotNone(after.forecast_captured_at)
        self.assertEqual(after.scout_team, before.scout_team)
        self.assertEqual(after.player_points, before.player_points)

    async def respond(self):
        with patch("main.scout", self.scout, create=True):
            return await _generate_scout_response(3, public=False)


if __name__ == "__main__":
    unittest.main()

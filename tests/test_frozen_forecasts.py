"""A gameweek's prediction must not change once its deadline has passed."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

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
        self.stats_revision = 0
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
                        "goals_scored": (
                            player_id + fixture["event"] + self.stats_revision
                        )
                        % 4,
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
        """GW3 goes live: its first match is played, earlier stats are
        corrected, a player is injured, ownership swings, and a match is
        postponed."""
        self.current = 3
        self.stats_revision = 1
        played = next(fixture for fixture in self.fixtures if fixture["id"] == 30)
        played.update(started=True, finished=True)
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

        # GW3's first result changes the history GW4 is predicted from, and
        # the injured player is no longer projected.
        injured = self.captain_id(squad_before)
        self.assertFalse(after.attrs["frozen"])
        self.assertEqual(after.set_index("id").loc[injured, "expected_points"], 0.0)
        self.assertNotEqual(records(after), records(before))

    def test_a_closed_gameweek_that_was_never_frozen_freezes_on_first_recall(self):
        self.now[0] = AFTER_GW3_DEADLINE
        first, squad_first = self.request()
        # Later data would change a live prediction: stats are corrected, the
        # captain is injured, ownership swings and a match is postponed.
        self.fpl.after_the_deadline(injured_player=self.captain_id(squad_first))
        self.client.clear_cache()
        self.monotonic[0] += 3600

        again, squad_again = self.request()
        response = asyncio.run(self.respond())

        self.assertTrue(first.attrs["frozen"])
        self.assertEqual(first.attrs["freeze_method"], "after-deadline")
        self.assertEqual(
            first.attrs["forecast_captured_at"], AFTER_GW3_DEADLINE.isoformat()
        )
        self.assertEqual(records(again), records(first))
        self.assertEqual(records(squad_again), records(squad_first))
        self.assertTrue(response.frozen)
        self.assertEqual(response.freeze_method, "after-deadline")
        self.assertEqual(response.scout_team, records(squad_first))

    def test_frozen_squad_does_not_depend_on_the_archived_squad_file(self):
        _, squad_before = self.request()
        # Squad files are archive records. The frozen squad is re-selected from
        # the frozen predictions, so a missing squad file changes nothing.
        (Path(self.directory.name) / "2026-2027/squads/gw_03.json").unlink()
        self.pass_the_deadline(injured_player=self.captain_id(squad_before))

        after, squad_after = self.request()

        self.assertTrue(after.attrs["frozen"])
        self.assertEqual(records(squad_after), records(squad_before))

    def test_an_older_versions_pre_deadline_forecast_is_what_freezes(self):
        self.now[0] = AFTER_GW3_DEADLINE
        live = self.predict_without_archive()
        # An older version archived this before the deadline; it is what
        # users saw, so it is frozen rather than today's prediction.
        pre_deadline = live.assign(expected_points=live["expected_points"] + 1.0)
        self.write_legacy_archive(pre_deadline, captured_at="2026-08-29T09:00:00+00:00")

        frozen, _ = self.request()
        again, _ = self.request()

        archived = pd.read_csv(self.season_root / "predictions/gw_03.csv")
        self.assertTrue(frozen.attrs["frozen"])
        self.assertEqual(frozen.attrs["freeze_method"], "after-deadline")
        self.assertEqual(frozen.attrs["forecast_captured_at"], "2026-08-29T09:00:00+00:00")
        self.assertEqual(records(frozen), records(archived))
        self.assertNotEqual(records(frozen), records(live))
        self.assertEqual(records(again), records(frozen))

    def test_an_older_versions_archive_from_after_the_deadline_is_not_frozen(self):
        self.now[0] = AFTER_GW3_DEADLINE
        live = self.predict_without_archive()
        # Older versions archived every request, even after the deadline.
        self.write_legacy_archive(
            live.assign(expected_points=99.0), captured_at="2026-08-30T11:00:00+00:00"
        )

        frozen, _ = self.request()

        self.assertTrue(frozen.attrs["frozen"])
        self.assertEqual(records(frozen), records(live))
        self.assertEqual(frozen.attrs["forecast_captured_at"], AFTER_GW3_DEADLINE.isoformat())
        # The training record an older version wrote is left as it was.
        archived = pd.read_csv(self.season_root / "predictions/gw_03.csv")
        self.assertTrue((archived["expected_points"] == 99.0).all())

    def test_with_the_archive_disabled_a_closed_gameweek_is_predicted_live(self):
        self.now[0] = AFTER_GW3_DEADLINE
        self.archive.enabled = False

        predictions, _ = self.request()

        self.assertFalse(predictions.attrs["frozen"])

    def test_a_failed_freeze_serves_the_live_prediction_and_retries(self):
        self.now[0] = AFTER_GW3_DEADLINE
        with patch.object(DataArchive, "_write_bytes", side_effect=OSError("disk full")):
            live = self.scout.get_official_predictions(3)
        frozen = self.scout.get_official_predictions(3)

        self.assertFalse(live.attrs["frozen"])
        self.assertTrue(frozen.attrs["frozen"])
        self.assertEqual(records(frozen), records(live))

    def test_a_later_freeze_never_replaces_the_first(self):
        self.now[0] = AFTER_GW3_DEADLINE
        first = self.scout.get_official_predictions(3)

        again = self.archive.freeze_after_deadline(
            self.client, 3, first.assign(expected_points=0.0)
        )

        self.assertEqual(records(again), records(first))
        self.assertEqual(records(self.scout.get_official_predictions(3)), records(first))

    def predict_without_archive(self, gameweek=3):
        """What a live prediction of the gameweek returns right now."""
        archive = self.scout.data_archive
        self.scout.data_archive = DataArchive(
            Path(self.directory.name) / "unused", enabled=False
        )
        try:
            return self.scout.get_official_predictions(gameweek)
        finally:
            self.scout.data_archive = archive

    def write_legacy_archive(self, predictions, captured_at, gameweek=3):
        """Files an older version archived: a predictions CSV and metadata
        with its capture time but no stored forecast."""
        (self.season_root / "predictions").mkdir(parents=True, exist_ok=True)
        (self.season_root / "metadata").mkdir(parents=True, exist_ok=True)
        predictions.to_csv(
            self.season_root / f"predictions/gw_{gameweek:02d}.csv", index=False
        )
        (self.season_root / f"metadata/gw_{gameweek:02d}.json").write_text(
            json.dumps(
                {
                    "archive_schema_version": 1,
                    "captured_at_utc": captured_at,
                    "prediction_gameweek": gameweek,
                    "source": "official-fpl",
                    "inference": {"strategy": "model-ensemble"},
                }
            ),
            encoding="utf-8",
        )

    @property
    def season_root(self):
        return Path(self.directory.name) / "2026-2027"

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

        predictions = self.scout.get_official_predictions()

        root = Path(self.directory.name) / "2026-2027/official"
        self.assertEqual(predictions.attrs["gameweek"], 4)
        self.assertTrue(predictions.attrs["frozen"])
        self.assertTrue((root / "player-stats/gw_04.csv").is_file())
        self.assertTrue((root / "live/gw_04.json").is_file())
        self.assertTrue((root / "history/before_gw_05.csv").is_file())

    def test_api_response_marks_the_frozen_forecast(self):
        before = asyncio.run(self.respond())
        _, squad_before = self.request()
        self.pass_the_deadline(injured_player=self.captain_id(squad_before))

        after = asyncio.run(self.respond())

        self.assertFalse(before.frozen)
        self.assertIsNone(before.freeze_method)
        self.assertTrue(after.frozen)
        self.assertEqual(after.freeze_method, "deadline")
        self.assertIsNotNone(after.forecast_captured_at)
        self.assertEqual(after.scout_team, before.scout_team)
        self.assertEqual(after.player_points, before.player_points)

    async def respond(self):
        with patch("main.scout", self.scout, create=True):
            return await _generate_scout_response(3, public=False)


if __name__ == "__main__":
    unittest.main()

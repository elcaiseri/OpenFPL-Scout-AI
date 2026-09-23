import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.data_archive import DataArchive

# GW1 is final, GW2 is in progress, and GW3 is open until its deadline.
BEFORE_GW3_DEADLINE = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)


def fixed_clock(moment=BEFORE_GW3_DEADLINE):
    return lambda: moment


class FakeOfficialClient:
    def __init__(self):
        self.live_calls = []
        self.history_calls = []

    def bootstrap(self):
        return {
            "events": [
                {
                    "id": 1,
                    "deadline_time": "2026-08-15T10:00:00Z",
                    "finished": True,
                    "data_checked": True,
                    "is_current": False,
                },
                {
                    "id": 2,
                    "deadline_time": "2026-08-22T10:00:00Z",
                    "finished": False,
                    "is_current": True,
                },
                {
                    "id": 3,
                    "deadline_time": "2026-08-29T10:00:00Z",
                    "finished": False,
                    "is_next": True,
                },
            ],
            "elements": [{"id": 10, "web_name": "Player"}],
            "teams": [{"id": 1, "name": "Arsenal"}],
            "total_players": 1000,
        }

    def fixtures(self):
        return [{"id": 100, "event": 3}]

    def event_live(self, gameweek):
        self.live_calls.append(gameweek)
        return {
            "elements": [
                {"id": 10, "stats": {"minutes": 90, "total_points": gameweek}}
            ]
        }

    def player_history(self, gameweek, selectable_only=True):
        self.history_calls.append((gameweek, selectable_only))
        return pd.DataFrame(
            [
                {
                    "id": 10,
                    "web_name": "Player",
                    "gameweek": 1,
                    "minutes": 90,
                    "total_points": 6,
                    "official_bps": 24,
                    "official_selected": 120,
                    "selected_by_percent": 45.0,
                }
            ]
        )


def prediction_frame(gameweek=3):
    frame = pd.DataFrame(
        [
            {
                "id": 10,
                "web_name": "Player",
                "gameweek": gameweek,
                "expected_points": 5.5,
            }
        ]
    )
    frame.attrs["gameweek"] = gameweek
    frame.attrs["source"] = "official-fpl+fpl-data"
    frame.attrs["inference"] = {
        "strategy": "model-ensemble",
        "successful_models": ["ridge"],
    }
    return frame


class DataArchiveTests(unittest.TestCase):
    def test_data_root_environment_moves_archive_to_mounted_directory(self):
        with patch.dict("os.environ", {"OPENFPL_DATA_ROOT": "/data"}, clear=False):
            archive = DataArchive.from_config(
                {"data_archive": {"enabled": True, "root_path": "data/archive"}}
            )

        self.assertEqual(archive.root_path, Path("/data/archive"))

    def test_writes_season_gameweek_training_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True, clock=fixed_clock())
            client = FakeOfficialClient()
            official = client.player_history(3)
            enriched = official.assign(total_shots=3)
            predictions = prediction_frame()

            result = archive.capture_inference(
                official_client=client,
                prediction_gameweek=3,
                official_history=official,
                enriched_history=enriched,
                predictions=predictions,
                source="official-fpl+fpl-data",
                enrichment={"provider": "fpl-data", "status": "applied"},
                model_versions={"ridge": {"version": "v1"}},
            )
            predictions.attrs["archive"] = result
            squad_result = archive.capture_squad(predictions, predictions)

            season = Path(directory) / "2026-2027"
            expected = [
                season / "official/snapshots/gw_03/bootstrap.json",
                season / "official/snapshots/gw_03/fixtures.json",
                season / "official/history/before_gw_02.csv",
                season / "official/player-stats/gw_01.csv",
                season / "official/live/gw_01.json",
                season / "official/live/gw_02.json",
                season / "enriched/history/before_gw_03.csv",
                season / "enriched/player-stats/gw_01.csv",
                season / "predictions/gw_03.csv",
                season / "metadata/gw_03.json",
                season / "squads/gw_03.json",
            ]
            self.assertEqual(result["status"], "saved")
            self.assertTrue(result["prediction_archived"])
            self.assertEqual(squad_result["status"], "saved")
            self.assertTrue(all(path.is_file() for path in expected))
            self.assertIn(
                "official_bps",
                pd.read_csv(season / "official/player-stats/gw_01.csv").columns,
            )
            self.assertIn(
                "total_shots",
                pd.read_csv(season / "enriched/player-stats/gw_01.csv").columns,
            )
            metadata = json.loads(
                (season / "metadata/gw_03.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["prediction_gameweek"], 3)
            self.assertEqual(metadata["official_history_before_gameweek"], 2)
            self.assertEqual(metadata["official_total_players"], 1000)
            self.assertEqual(metadata["live_gameweeks"], [1, 2])
            self.assertEqual(client.live_calls, [1, 2])

    def test_does_not_refetch_finalized_live_gameweek(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True, clock=fixed_clock())
            client = FakeOfficialClient()
            history = client.player_history(3)
            predictions = prediction_frame()
            arguments = {
                "official_client": client,
                "prediction_gameweek": 3,
                "official_history": history,
                "enriched_history": history,
                "predictions": predictions,
                "source": "official-fpl",
                "enrichment": {"status": "disabled"},
                "model_versions": {},
            }

            archive.capture_inference(**arguments)
            archive.capture_inference(**arguments)

            self.assertEqual(client.live_calls, [1, 2, 2])

    def test_refetches_finished_live_gameweek_until_data_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True, clock=fixed_clock())
            client = FakeOfficialClient()
            payload = client.bootstrap()
            payload["events"][0]["data_checked"] = False
            client.bootstrap = lambda: payload
            history = client.player_history(3)
            arguments = {
                "official_client": client,
                "prediction_gameweek": 3,
                "official_history": history,
                "enriched_history": history,
                "predictions": prediction_frame(),
                "source": "official-fpl",
                "enrichment": {"status": "disabled"},
                "model_versions": {},
            }

            archive.capture_inference(**arguments)
            archive.capture_inference(**arguments)
            payload["events"][0]["data_checked"] = True
            archive.capture_inference(**arguments)
            archive.capture_inference(**arguments)

            # Fetched on both captures before the data check, then frozen.
            self.assertEqual(client.live_calls.count(1), 2)

    def test_requests_for_closed_gameweeks_never_replace_archived_forecasts(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True, clock=fixed_clock())
            client = FakeOfficialClient()
            season = Path(directory) / "2026-2027"
            earlier_forecast = season / "predictions/gw_02.csv"
            earlier_forecast.parent.mkdir(parents=True)
            earlier_forecast.write_text("id,expected_points\n10,4.0\n")
            history = client.player_history(2)

            for gameweek in (1, 2, 5):
                with self.subTest(gameweek=gameweek):
                    predictions = prediction_frame(gameweek)
                    result = archive.capture_inference(
                        official_client=client,
                        prediction_gameweek=gameweek,
                        official_history=history,
                        enriched_history=history,
                        predictions=predictions,
                        source="official-fpl",
                        enrichment={"status": "disabled"},
                        model_versions={},
                    )
                    predictions.attrs["archive"] = result
                    squad_result = archive.capture_squad(predictions, predictions)

                    self.assertEqual(result["status"], "saved")
                    self.assertFalse(result["prediction_archived"])
                    self.assertIn("open: 3", result["prediction_skipped_reason"])
                    self.assertEqual(squad_result["status"], "skipped")
                    for folder in ("metadata", "squads"):
                        target = season / folder / f"gw_{gameweek:02d}.json"
                        self.assertFalse(target.exists())

            self.assertEqual(
                earlier_forecast.read_text(), "id,expected_points\n10,4.0\n"
            )
            self.assertTrue((season / "official/history/before_gw_02.csv").is_file())

    def test_archived_history_does_not_present_capture_ownership_as_historical(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True, clock=fixed_clock())
            client = FakeOfficialClient()
            history = client.player_history(3)

            archive.capture_inference(
                official_client=client,
                prediction_gameweek=3,
                official_history=history,
                enriched_history=history,
                predictions=prediction_frame(),
                source="official-fpl+fpl-data",
                enrichment={"status": "applied"},
                model_versions={},
            )

            season = Path(directory) / "2026-2027"
            for relative in (
                "official/history/before_gw_02.csv",
                "official/player-stats/gw_01.csv",
                "enriched/history/before_gw_03.csv",
            ):
                with self.subTest(file=relative):
                    frame = pd.read_csv(season / relative)
                    self.assertTrue(frame["selected_by_percent"].isna().all())
                    self.assertEqual(
                        frame["selected_by_percent_at_capture"].tolist(), [45.0]
                    )
                    self.assertEqual(frame["official_selected"].tolist(), [120])

    def test_enriched_stats_skip_gameweeks_fpl_data_has_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True, clock=fixed_clock())
            client = FakeOfficialClient()
            history = pd.concat(
                [client.player_history(3), client.player_history(3).assign(gameweek=2)],
                ignore_index=True,
            )

            archive.capture_inference(
                official_client=client,
                prediction_gameweek=3,
                official_history=history,
                enriched_history=history,
                predictions=prediction_frame(),
                source="official-fpl+fpl-data",
                enrichment={"status": "applied", "unenriched_gameweeks": [2]},
                model_versions={},
            )

            stats = Path(directory) / "2026-2027/enriched/player-stats"
            self.assertTrue((stats / "gw_01.csv").is_file())
            self.assertFalse((stats / "gw_02.csv").exists())

    def test_unchanged_squad_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True, clock=fixed_clock())
            client = FakeOfficialClient()
            history = client.player_history(3)
            predictions = prediction_frame()
            predictions.attrs["archive"] = archive.capture_inference(
                official_client=client,
                prediction_gameweek=3,
                official_history=history,
                enriched_history=history,
                predictions=predictions,
                source="official-fpl",
                enrichment={"status": "disabled"},
                model_versions={},
            )

            first = archive.capture_squad(predictions, predictions)
            second = archive.capture_squad(predictions, predictions)

            self.assertEqual(first["status"], "saved")
            self.assertEqual(second["status"], "unchanged")

    def test_archive_failure_does_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root_file = Path(directory) / "not-a-directory"
            root_file.write_text("occupied", encoding="utf-8")
            archive = DataArchive(root_file, enabled=True, clock=fixed_clock())
            history = FakeOfficialClient().player_history(3)

            result = archive.capture_inference(
                official_client=FakeOfficialClient(),
                prediction_gameweek=3,
                official_history=history,
                enriched_history=history,
                predictions=prediction_frame(),
                source="official-fpl",
                enrichment={"status": "disabled"},
                model_versions={},
            )

            self.assertEqual(result["status"], "failed")
            self.assertIn("Not a directory", result["error"])

    def test_final_gameweek_uses_history_cutoff_39(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True, clock=fixed_clock())
            client = FakeOfficialClient()
            payload = client.bootstrap()
            payload["events"] = [
                {
                    "id": gameweek,
                    "deadline_time": f"2026-08-{min(gameweek, 28):02d}T10:00:00Z",
                    "finished": True,
                    "is_current": gameweek == 38,
                }
                for gameweek in range(1, 39)
            ]
            client.bootstrap = lambda: payload
            history = client.player_history(38)

            archive.capture_inference(
                official_client=client,
                prediction_gameweek=38,
                official_history=history,
                enriched_history=history,
                predictions=prediction_frame(),
                source="official-fpl",
                enrichment={"status": "disabled"},
                model_versions={},
            )

            self.assertIn((39, False), client.history_calls)
            self.assertTrue(
                (
                    Path(directory)
                    / "2026-2027/official/history/before_gw_39.csv"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()

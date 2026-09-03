import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.data_archive import DataArchive


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
                    "is_current": False,
                },
                {
                    "id": 2,
                    "deadline_time": "2026-08-22T10:00:00Z",
                    "finished": False,
                    "is_current": True,
                },
            ],
            "elements": [{"id": 10, "web_name": "Player"}],
            "teams": [{"id": 1, "name": "Arsenal"}],
        }

    def fixtures(self):
        return [{"id": 100, "event": 2}]

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
                }
            ]
        )


def prediction_frame():
    frame = pd.DataFrame(
        [
            {
                "id": 10,
                "web_name": "Player",
                "gameweek": 2,
                "expected_points": 5.5,
            }
        ]
    )
    frame.attrs["gameweek"] = 2
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
            archive = DataArchive(Path(directory), enabled=True)
            client = FakeOfficialClient()
            official = client.player_history(2)
            enriched = official.assign(total_shots=3)
            predictions = prediction_frame()

            result = archive.capture_inference(
                official_client=client,
                prediction_gameweek=2,
                official_history=official,
                enriched_history=enriched,
                predictions=predictions,
                source="official-fpl+fpl-data",
                enrichment={"provider": "fpl-data", "status": "applied"},
                model_versions={"ridge": {"version": "v1"}},
            )
            squad_result = archive.capture_squad(predictions, predictions)

            season = Path(directory) / "2026-2027"
            expected = [
                season / "official/snapshots/gw_02/bootstrap.json",
                season / "official/snapshots/gw_02/fixtures.json",
                season / "official/history/before_gw_02.csv",
                season / "official/player-stats/gw_01.csv",
                season / "official/live/gw_01.json",
                season / "official/live/gw_02.json",
                season / "enriched/history/before_gw_02.csv",
                season / "enriched/player-stats/gw_01.csv",
                season / "predictions/gw_02.csv",
                season / "metadata/gw_02.json",
                season / "squads/gw_02.json",
            ]
            self.assertEqual(result["status"], "saved")
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
                (season / "metadata/gw_02.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["prediction_gameweek"], 2)
            self.assertEqual(metadata["live_gameweeks"], [1, 2])
            self.assertEqual(client.live_calls, [1, 2])

    def test_does_not_refetch_finalized_live_gameweek(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory), enabled=True)
            client = FakeOfficialClient()
            history = client.player_history(2)
            predictions = prediction_frame()
            arguments = {
                "official_client": client,
                "prediction_gameweek": 2,
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

    def test_archive_failure_does_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root_file = Path(directory) / "not-a-directory"
            root_file.write_text("occupied", encoding="utf-8")
            archive = DataArchive(root_file, enabled=True)
            history = FakeOfficialClient().player_history(2)

            result = archive.capture_inference(
                official_client=FakeOfficialClient(),
                prediction_gameweek=2,
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
            archive = DataArchive(Path(directory), enabled=True)
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

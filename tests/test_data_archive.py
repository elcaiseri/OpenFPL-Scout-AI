import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
            moment = [BEFORE_GW3_DEADLINE]
            archive = DataArchive(
                Path(directory), enabled=True, clock=lambda: moment[0]
            )
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
            # GW1 settled 30 minutes after its data check was first seen.
            moment[0] += timedelta(minutes=31)
            archive.capture_inference(**arguments)

            self.assertEqual(client.live_calls, [1, 2, 2])

    def test_refetches_finished_live_gameweek_until_it_settles_after_data_check(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            moment = [BEFORE_GW3_DEADLINE]
            archive = DataArchive(
                Path(directory), enabled=True, clock=lambda: moment[0]
            )
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
            moment[0] += timedelta(minutes=10)
            archive.capture_inference(**arguments)
            moment[0] += timedelta(minutes=21)
            archive.capture_inference(**arguments)

            # Fetched on both captures before the data check and until 30
            # minutes after it was first seen, then frozen.
            self.assertEqual(client.live_calls.count(1), 4)

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

    def test_cached_predictions_never_write_a_squad_after_the_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            moment = [datetime(2026, 8, 29, 9, 58, tzinfo=timezone.utc)]
            archive = DataArchive(
                Path(directory), enabled=True, clock=lambda: moment[0]
            )
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

            # The same cached predictions are served two minutes after the
            # GW3 deadline (10:00 UTC).
            moment[0] = datetime(2026, 8, 29, 10, 2, tzinfo=timezone.utc)
            result = archive.capture_squad(predictions, predictions)

            self.assertEqual(
                predictions.attrs["archive"]["open_until_utc"],
                "2026-08-29T10:00:00+00:00",
            )
            self.assertEqual(result, {"status": "skipped", "reason": "gameweek-closed"})
            self.assertFalse((Path(directory) / "2026-2027/squads/gw_03.json").exists())

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

    def test_a_capture_that_crosses_the_deadline_never_replaces_the_forecast(self):
        with tempfile.TemporaryDirectory() as directory:
            moment = [datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)]
            archive = DataArchive(
                Path(directory), enabled=True, clock=lambda: moment[0]
            )
            client = FakeOfficialClient()
            self.capture(archive, client, prediction_frame())
            later = prediction_frame()
            later["expected_points"] = 9.5
            fetch_fixtures = client.fixtures

            def fixtures_while_the_deadline_passes():
                moment[0] = datetime(2026, 8, 29, 10, 1, tzinfo=timezone.utc)
                return fetch_fixtures()

            client.fixtures = fixtures_while_the_deadline_passes
            moment[0] = datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc)
            result = self.capture(archive, client, later)
            frozen = archive.frozen_forecast(client, 3)

            self.assertFalse(result["prediction_archived"])
            self.assertEqual(result["prediction_skipped_reason"], "gameweek-closed")
            self.assertEqual(frozen["expected_points"].tolist(), [5.5])
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])

    def test_instances_sharing_the_volume_leave_a_consistent_forecast(self):
        with tempfile.TemporaryDirectory() as directory:
            moment = [datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)]
            first, second = (
                DataArchive(Path(directory), enabled=True, clock=lambda: moment[0])
                for _ in range(2)
            )
            client = FakeOfficialClient()
            # The second instance saw a rescheduled fixture list.
            other_client = FakeOfficialClient()
            other_client.fixtures = lambda: [{"id": 100, "event": 4}]
            other = prediction_frame()
            other["expected_points"] = 9.5
            self.capture(first, client, prediction_frame())
            moment[0] = datetime(2026, 8, 29, 9, 10, tzinfo=timezone.utc)
            self.capture(second, other_client, other)
            # The first instance predicts the same values from the same inputs.
            moment[0] = datetime(2026, 8, 29, 9, 20, tzinfo=timezone.utc)
            self.capture(first, client, prediction_frame())
            moment[0] = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)

            frozen = second.frozen_forecast(client, 3)
            snapshot = json.loads(
                (
                    Path(directory) / "2026-2027/official/snapshots/gw_03/fixtures.json"
                ).read_text(encoding="utf-8")
            )

            self.assertIsNotNone(frozen)
            self.assertEqual(frozen["expected_points"].tolist(), [5.5])
            self.assertEqual(snapshot, client.fixtures())

    def test_live_results_are_refetched_until_well_after_the_data_check(self):
        with tempfile.TemporaryDirectory() as directory:
            moment = [datetime(2026, 8, 20, 12, tzinfo=timezone.utc)]
            archive = DataArchive(
                Path(directory), enabled=True, clock=lambda: moment[0]
            )
            client = FakeOfficialClient()
            payload = client.bootstrap()
            payload["events"][0]["data_checked"] = False
            client.bootstrap = lambda: payload
            points = [3]
            client.event_live = lambda gameweek: {
                "elements": [{"id": 10, "stats": {"total_points": points[0]}}]
            }
            live = Path(directory) / "2026-2027/official/live/gw_01.json"
            read = lambda: json.loads(live.read_text(encoding="utf-8"))  # noqa: E731

            archive.collect_results(client)
            # Bonus points are corrected, then FPL marks GW1 data_checked.
            points[0] = 4
            payload["events"][0]["data_checked"] = True
            moment[0] += timedelta(minutes=10)
            archive.collect_results(client)
            corrected = read()["elements"][0]["stats"]["total_points"]
            # Long after the check, the file is final and no longer fetched.
            points[0] = 99
            moment[0] += timedelta(hours=1)
            archive.collect_results(client)

            self.assertEqual(corrected, 4)
            self.assertEqual(read()["elements"][0]["stats"]["total_points"], 4)

    def test_forecasts_stop_being_committed_shortly_before_the_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            moment = [datetime(2026, 8, 29, 9, 58, 59, tzinfo=timezone.utc)]
            archive = DataArchive(
                Path(directory), enabled=True, clock=lambda: moment[0]
            )
            client = FakeOfficialClient()
            accepted = self.capture(archive, client, prediction_frame())
            moment[0] = datetime(2026, 8, 29, 9, 59, 30, tzinfo=timezone.utc)
            later = prediction_frame()
            later["expected_points"] = 9.5
            refused = self.capture(archive, client, later)
            moment[0] = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)

            self.assertTrue(accepted["prediction_archived"])
            self.assertFalse(refused["prediction_archived"])
            self.assertEqual(
                archive.frozen_forecast(client, 3)["expected_points"].tolist(), [5.5]
            )

    def test_frozen_forecast_requires_proof_it_was_captured_before_the_deadline(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            moment = [datetime(2026, 8, 28, 12, tzinfo=timezone.utc)]
            archive = DataArchive(
                Path(directory), enabled=True, clock=lambda: moment[0]
            )
            client = FakeOfficialClient()
            self.capture(archive, client, prediction_frame())
            moment[0] = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
            root = Path(directory) / "2026-2027"
            metadata_path = root / "metadata/gw_03.json"
            predictions_path = root / "predictions/gw_03.csv"
            captured = json.loads(metadata_path.read_text(encoding="utf-8"))
            csv = predictions_path.read_bytes()
            self.assertIsNotNone(archive.frozen_forecast(client, 3))

            cases = {
                # The previous version archived requests after the deadline.
                "legacy, after the deadline": {
                    "archive_schema_version": 1,
                    "captured_at_utc": "2026-09-25T11:00:00+00:00",
                    "prediction_gameweek": 3,
                },
                "legacy, no digest": {
                    **captured,
                    "archive_schema_version": 1,
                    "predictions_sha256": None,
                },
                "captured after the deadline": {
                    **captured,
                    "captured_at_utc": "2026-08-29T10:00:00+00:00",
                },
                "another gameweek": {**captured, "prediction_gameweek": 4},
            }
            for name, metadata in cases.items():
                with self.subTest(name):
                    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                    self.assertIsNone(archive.frozen_forecast(client, 3))

            with self.subTest("predictions replaced"):
                metadata_path.write_text(json.dumps(captured), encoding="utf-8")
                predictions_path.write_bytes(csv.replace(b"5.5", b"9.5"))
                self.assertIsNone(archive.frozen_forecast(client, 3))

    def test_results_are_collected_without_a_prediction_at_most_once_per_interval(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            moment = [datetime(2026, 8, 30, 12, tzinfo=timezone.utc)]
            archive = DataArchive(
                Path(directory),
                enabled=True,
                clock=lambda: moment[0],
                results_interval_seconds=300,
            )
            client = FakeOfficialClient()
            stats = Path(directory) / "2026-2027/official/player-stats/gw_01.csv"

            first = archive.collect_results(client)
            stats.unlink()
            throttled = archive.collect_results(client)
            moment[0] = datetime(2026, 8, 30, 12, 5, tzinfo=timezone.utc)
            archive._digests.clear()
            again = archive.collect_results(client)

            self.assertEqual(first["status"], "saved")
            self.assertIn("official/history/before_gw_02.csv", first["files_updated"])
            self.assertIn("official/live/gw_02.json", first["files_updated"])
            self.assertEqual(throttled["status"], "throttled")
            self.assertEqual(again["status"], "saved")
            self.assertTrue(stats.is_file())
            self.assertFalse((Path(directory) / "2026-2027/predictions").exists())

    @staticmethod
    def capture(archive, client, predictions):
        history = client.player_history(3)
        return archive.capture_inference(
            official_client=client,
            prediction_gameweek=3,
            official_history=history,
            enriched_history=history,
            predictions=predictions,
            source="official-fpl",
            enrichment={"status": "disabled"},
            model_versions={},
        )


if __name__ == "__main__":
    unittest.main()

"""An open gameweek is predicted once and reused, not rerun per request."""

import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src.data_archive import DataArchive
from src.official_fpl import OfficialFPLClient
from src.scout import FPLScout
from tests.test_frozen_forecasts import (
    AFTER_GW3_DEADLINE,
    BEFORE_GW3_DEADLINE,
    FakeFPL,
    GoalsModel,
    records,
)


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = [BEFORE_GW3_DEADLINE]
        self.monotonic = [0.0]
        self.fpl = FakeFPL()
        self.scout = self.new_instance()

    def new_instance(self, **inference):
        """A service instance sharing the archive volume, as after a restart."""
        client = OfficialFPLClient(session=self.fpl)
        archive = DataArchive(
            Path(self.directory.name),
            configured_season="2026-2027",
            clock=lambda: self.now[0],
        )
        scout = FPLScout(
            {
                "inference": {
                    "minimum_successful_models": 1,
                    "prediction_cache_ttl_seconds": 300,
                    "prediction_stale_seconds": 900,
                    "prediction_restore_max_age_seconds": 3600,
                    **inference,
                },
                "fpl_data_inference": {"enabled": False},
                "models": {"model": {"path": "model.pkl"}},
            },
            model_loader=lambda path: GoalsModel(),
            official_client=client,
            data_archive=archive,
        )
        scout.clock = lambda: self.monotonic[0]
        # Background predictions write to the archive directory, so they
        # finish before it is removed.
        self.addCleanup(scout.close, wait=True)
        return scout

    def predictions_made(self, scout=None):
        """Counts ensemble runs (history loads also happen when archiving)."""
        scout = scout or self.scout
        return patch.object(scout, "_predict_live", wraps=scout._predict_live)

    def change_official_data(self):
        """Official FPL corrects match stats, which changes the forecast."""
        self.fpl.stats_revision += 1
        self.scout.official_client.clear_cache()

    def finish_background_prediction(self, gameweek=3):
        with self.scout._forecast_lock:
            pending = self.scout._pending_forecasts.get(gameweek)
        if pending is not None:
            pending.result()


class PredictionCacheTests(CacheTestCase):
    def test_repeat_requests_reuse_one_prediction(self):
        with self.predictions_made() as predicted:
            first = self.scout.get_official_predictions(3)
            self.monotonic[0] += 299
            second = self.scout.get_official_predictions(3)

        self.assertEqual(predicted.call_count, 1)
        self.assertEqual(records(second), records(first))
        self.assertFalse(second.attrs["frozen"])
        self.assertEqual(second.attrs["gameweek"], 3)

    def test_each_request_gets_its_own_copy(self):
        first = self.scout.get_official_predictions(3)
        first["expected_points"] = -1.0
        first.attrs["inference"]["strategy"] = "changed"

        second = self.scout.get_official_predictions(3)

        self.assertTrue((second["expected_points"] >= 0).all())
        self.assertEqual(second.attrs["inference"]["strategy"], "model-ensemble")

    def test_a_stale_forecast_is_served_while_a_new_one_is_predicted(self):
        first = self.scout.get_official_predictions(3)
        self.change_official_data()
        self.monotonic[0] += 301

        stale = self.scout.get_official_predictions(3)
        self.finish_background_prediction()
        fresh = self.scout.get_official_predictions(3)

        self.assertEqual(records(stale), records(first))
        self.assertNotEqual(records(fresh), records(first))

    def test_a_forecast_past_the_stale_limit_is_predicted_again_first(self):
        first = self.scout.get_official_predictions(3)
        self.change_official_data()
        self.monotonic[0] += 901

        second = self.scout.get_official_predictions(3)

        self.assertNotEqual(records(second), records(first))

    def test_simultaneous_requests_share_one_prediction(self):
        started = threading.Event()
        release = threading.Event()
        load = self.scout.official_client.player_history

        def slow_history(*args, **kwargs):
            started.set()
            release.wait(5)
            return load(*args, **kwargs)

        results = []
        with patch.object(
            self.scout.official_client, "player_history", side_effect=slow_history
        ), self.predictions_made() as predicted:
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        self.scout.get_official_predictions(3)
                    )
                )
                for _ in range(3)
            ]
            threads[0].start()
            started.wait(5)
            for thread in threads[1:]:
                thread.start()
            release.set()
            for thread in threads:
                thread.join(5)

        self.assertEqual(predicted.call_count, 1)
        self.assertEqual(len(results), 3)
        self.assertEqual(records(results[1]), records(results[0]))
        self.assertEqual(records(results[2]), records(results[0]))

    def test_a_failed_refresh_keeps_serving_the_previous_forecast(self):
        first = self.scout.get_official_predictions(3)
        self.monotonic[0] += 301
        with patch.object(
            self.scout.official_client,
            "player_history",
            side_effect=RuntimeError("FPL unavailable"),
        ):
            stale = self.scout.get_official_predictions(3)
            with self.assertRaises(RuntimeError):
                self.finish_background_prediction()
            again = self.scout.get_official_predictions(3)

        self.assertEqual(records(stale), records(first))
        self.assertEqual(records(again), records(first))

    def test_a_failed_prediction_is_reported_and_not_cached(self):
        with patch.object(
            self.scout.official_client,
            "player_history",
            side_effect=RuntimeError("FPL unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.scout.get_official_predictions(3)

        predictions = self.scout.get_official_predictions(3)

        self.assertEqual(predictions.attrs["gameweek"], 3)

    def test_a_cached_forecast_is_never_served_after_the_deadline(self):
        self.scout.data_archive.enabled = False
        before = self.scout.get_official_predictions(3)
        self.now[0] = AFTER_GW3_DEADLINE
        self.change_official_data()

        with self.predictions_made() as predicted:
            after = self.scout.get_official_predictions(3)

        self.assertEqual(predicted.call_count, 1)
        self.assertNotEqual(records(after), records(before))

    def test_a_zero_ttl_predicts_every_request(self):
        scout = self.new_instance(
            prediction_cache_ttl_seconds=0, prediction_stale_seconds=0
        )
        with self.predictions_made(scout) as predicted:
            scout.get_official_predictions(3)
            scout.get_official_predictions(3)

        self.assertEqual(predicted.call_count, 2)

    def test_stale_limit_must_cover_the_ttl(self):
        with self.assertRaises(ValueError):
            self.new_instance(
                prediction_cache_ttl_seconds=300, prediction_stale_seconds=60
            )


class ColdStartTests(CacheTestCase):
    """A new instance serves the last archived forecast while it predicts."""

    def restart(self, after=timedelta(minutes=20)):
        self.now[0] += after
        self.change_official_data()
        self.scout = self.new_instance()

    def test_a_new_instance_answers_from_the_archive_then_predicts(self):
        archived = self.scout.get_official_predictions(3)
        self.restart()

        with self.predictions_made() as predicted:
            restored = self.scout.get_official_predictions(3)
            self.finish_background_prediction()
            fresh = self.scout.get_official_predictions(3)

        self.assertEqual(predicted.call_count, 1)
        self.assertEqual(records(restored), records(archived))
        self.assertFalse(restored.attrs["frozen"])
        self.assertEqual(restored.attrs["inference"]["strategy"], "model-ensemble")
        self.assertNotEqual(records(fresh), records(archived))

    def test_a_restored_forecast_selects_the_same_squad(self):
        archived = self.scout.get_official_predictions(3)
        squad = self.scout.select_optimal_team(archived)
        self.restart()

        restored = self.scout.get_official_predictions(3)

        self.assertEqual(
            records(self.scout.select_optimal_team(restored)), records(squad)
        )

    def test_a_restored_forecast_is_not_archived_as_this_gameweeks_squad(self):
        self.scout.get_official_predictions(3)
        self.restart()

        restored = self.scout.get_official_predictions(3)
        result = self.scout.data_archive.capture_squad(
            restored, self.scout.select_optimal_team(restored)
        )

        self.assertEqual(result["status"], "skipped")

    def test_a_forecast_from_other_models_is_not_restored(self):
        archived = self.scout.get_official_predictions(3)
        self.restart()
        # A deploy shipped new models since the forecast was archived.
        self.scout.config["models"]["model"]["version"] = "retrained"

        predictions = self.scout.get_official_predictions(3)

        self.assertNotEqual(records(predictions), records(archived))

    def test_an_old_archived_forecast_is_not_restored(self):
        archived = self.scout.get_official_predictions(3)
        self.restart(after=timedelta(hours=2))

        predictions = self.scout.get_official_predictions(3)

        self.assertNotEqual(records(predictions), records(archived))


if __name__ == "__main__":
    unittest.main()

"""An open gameweek is predicted once and reused, not rerun per request.

Every prediction runs in the thread of a request: the web page may be served
an older forecast at once, and then asks for the refresh in a request of its
own, as request-billed Cloud Run only gives CPU to requests.
"""

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
        return scout

    def web(self, gameweek=3, scout=None):
        """The public squad request, which may take an older forecast."""
        scout = scout or self.scout
        return scout.get_official_predictions(
            gameweek, collect_results=False, allow_stale=True
        )

    def predictions_made(self, scout=None):
        """Counts ensemble runs (history loads also happen when archiving)."""
        scout = scout or self.scout
        return patch.object(scout, "_predict_live", wraps=scout._predict_live)

    def change_official_data(self):
        """Official FPL corrects match stats, which changes the forecast."""
        self.fpl.stats_revision += 1
        self.scout.official_client.clear_cache()

    def change_availability(self, predictions, **official):
        """Official news about the forecast's top player; returns their id."""
        player_id = int(
            predictions.sort_values("expected_points", ascending=False).iloc[0]["id"]
        )
        self.fpl.elements[player_id - 1].update(official)
        self.scout.official_client.clear_cache()
        return player_id


class PredictionCacheTests(CacheTestCase):
    def test_repeat_requests_reuse_one_prediction(self):
        with self.predictions_made() as predicted:
            first = self.scout.get_official_predictions(3)
            self.monotonic[0] += 299
            second = self.scout.get_official_predictions(3)

        self.assertEqual(predicted.call_count, 1)
        self.assertEqual(records(second), records(first))
        self.assertFalse(second.attrs["frozen"])
        self.assertFalse(second.attrs["refresh_due"])
        self.assertEqual(second.attrs["gameweek"], 3)

    def test_each_request_gets_its_own_copy(self):
        first = self.scout.get_official_predictions(3)
        first["expected_points"] = -1.0
        first.attrs["inference"]["strategy"] = "changed"

        second = self.scout.get_official_predictions(3)

        self.assertTrue((second["expected_points"] >= 0).all())
        self.assertEqual(second.attrs["inference"]["strategy"], "model-ensemble")

    def test_the_page_gets_a_stale_forecast_and_refreshes_it_in_its_own_request(self):
        first = self.web()
        self.change_official_data()
        self.monotonic[0] += 301
        threads = threading.active_count()

        with self.predictions_made() as predicted:
            stale = self.web()
            # Nothing predicts until the refresh request arrives.
            self.assertEqual(predicted.call_count, 0)
            self.assertEqual(threading.active_count(), threads)
            status = self.scout.refresh_predictions(3)
            fresh = self.web()

        self.assertEqual(records(stale), records(first))
        self.assertTrue(stale.attrs["refresh_due"])
        self.assertEqual(status, "refreshed")
        self.assertEqual(predicted.call_count, 1)
        self.assertNotEqual(records(fresh), records(first))
        self.assertFalse(fresh.attrs["refresh_due"])
        self.assertEqual(self.scout.refresh_predictions(3), "fresh")

    def test_callers_that_cannot_take_a_stale_forecast_predict_first(self):
        first = self.scout.get_official_predictions(3)
        self.change_official_data()
        self.monotonic[0] += 301

        second = self.scout.get_official_predictions(3)

        self.assertNotEqual(records(second), records(first))
        self.assertFalse(second.attrs["refresh_due"])

    def test_a_forecast_past_the_stale_limit_is_predicted_again_first(self):
        first = self.web()
        self.change_official_data()
        self.monotonic[0] += 901

        second = self.web()

        self.assertNotEqual(records(second), records(first))
        self.assertFalse(second.attrs["refresh_due"])

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
        first = self.web()
        self.monotonic[0] += 301
        with patch.object(
            self.scout.official_client,
            "player_history",
            side_effect=RuntimeError("FPL unavailable"),
        ):
            stale = self.web()
            with self.assertRaises(RuntimeError):
                self.scout.refresh_predictions(3)
            again = self.web()

        self.assertEqual(records(stale), records(first))
        self.assertEqual(records(again), records(first))
        self.assertTrue(again.attrs["refresh_due"])

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
        before = self.web()
        self.now[0] = AFTER_GW3_DEADLINE
        self.change_official_data()

        with self.predictions_made() as predicted:
            after = self.web()

        self.assertEqual(predicted.call_count, 1)
        self.assertNotEqual(records(after), records(before))
        self.assertEqual(self.scout.refresh_predictions(3), "not-cached")

    def test_a_zero_ttl_predicts_every_request(self):
        scout = self.new_instance(
            prediction_cache_ttl_seconds=0, prediction_stale_seconds=0
        )
        with self.predictions_made(scout) as predicted:
            self.web(scout=scout)
            self.web(scout=scout)

        self.assertEqual(predicted.call_count, 2)
        self.assertEqual(scout.refresh_predictions(3), "not-cached")

    def test_stale_limit_must_cover_the_ttl(self):
        with self.assertRaises(ValueError):
            self.new_instance(
                prediction_cache_ttl_seconds=300, prediction_stale_seconds=60
            )


class AvailabilityTests(CacheTestCase):
    """Official news drops a cached forecast at once, however recent."""

    def test_an_injury_is_predicted_again_within_the_ttl(self):
        first = self.scout.get_official_predictions(3)
        injured = self.change_availability(
            first, status="i", chance_of_playing_next_round=0
        )

        with self.predictions_made() as predicted:
            second = self.scout.get_official_predictions(3)

        self.assertEqual(predicted.call_count, 1)
        player = second.set_index("id").loc[injured]
        self.assertEqual(player["availability_factor"], 0.0)
        self.assertEqual(player["expected_points"], 0.0)

    def test_the_page_never_gets_a_stale_forecast_from_before_the_news(self):
        first = self.web()
        self.monotonic[0] += 301
        departed = self.change_availability(first, can_select=False)

        second = self.web()

        self.assertFalse(second.attrs["refresh_due"])
        # A player FPL no longer lets managers select is not predicted at all.
        self.assertIn(departed, set(first["id"]))
        self.assertNotIn(departed, set(second["id"]))

    def test_availability_is_checked_once_per_official_update(self):
        self.scout.get_official_predictions(3)
        with patch.object(
            self.scout, "_availability_changed", wraps=self.scout._availability_changed
        ) as checked:
            for _ in range(3):
                self.scout.get_official_predictions(3)
            self.assertEqual(checked.call_count, 1)
            # New official data is checked again.
            self.scout.official_client.clear_cache()
            self.scout.get_official_predictions(3)

        self.assertEqual(checked.call_count, 2)

    def test_other_official_changes_keep_the_cached_forecast(self):
        first = self.scout.get_official_predictions(3)
        self.change_official_data()

        with self.predictions_made() as predicted:
            second = self.scout.get_official_predictions(3)

        self.assertEqual(predicted.call_count, 0)
        self.assertEqual(records(second), records(first))


class ColdStartTests(CacheTestCase):
    """A new instance gives the page the last archived forecast at once."""

    def restart(self, after=timedelta(minutes=20)):
        self.now[0] += after
        self.change_official_data()
        self.scout = self.new_instance()

    def test_a_new_instance_answers_from_the_archive_then_refreshes(self):
        archived = self.web()
        self.restart()

        with self.predictions_made() as predicted:
            restored = self.web()
            self.assertEqual(predicted.call_count, 0)
            self.scout.refresh_predictions(3)
            fresh = self.web()

        self.assertEqual(predicted.call_count, 1)
        self.assertEqual(records(restored), records(archived))
        self.assertTrue(restored.attrs["refresh_due"])
        self.assertFalse(restored.attrs["frozen"])
        self.assertEqual(restored.attrs["inference"]["strategy"], "model-ensemble")
        self.assertNotEqual(records(fresh), records(archived))
        self.assertFalse(fresh.attrs["refresh_due"])

    def test_callers_that_cannot_take_a_stale_forecast_are_not_given_one(self):
        archived = self.web()
        self.restart()

        predictions = self.scout.get_official_predictions(3)

        self.assertNotEqual(records(predictions), records(archived))
        self.assertFalse(predictions.attrs["refresh_due"])

    def test_a_restored_forecast_selects_the_same_squad(self):
        archived = self.web()
        squad = self.scout.select_optimal_team(archived)
        self.restart()

        restored = self.web()

        self.assertEqual(
            records(self.scout.select_optimal_team(restored)), records(squad)
        )

    def test_a_restored_forecast_is_not_archived_as_this_gameweeks_squad(self):
        self.web()
        self.restart()

        restored = self.web()
        result = self.scout.data_archive.capture_squad(
            restored, self.scout.select_optimal_team(restored)
        )

        self.assertEqual(result["status"], "skipped")

    def test_a_forecast_from_before_availability_news_is_not_restored(self):
        archived = self.web()
        self.restart()
        injured = self.change_availability(
            archived, status="s", chance_of_playing_next_round=0
        )

        predictions = self.web()

        self.assertFalse(predictions.attrs["refresh_due"])
        self.assertEqual(
            predictions.set_index("id").loc[injured, "availability_factor"], 0.0
        )

    def test_a_forecast_from_other_models_is_not_restored(self):
        archived = self.web()
        self.restart()
        # A deploy shipped new models since the forecast was archived.
        self.scout.config["models"]["model"]["version"] = "retrained"

        predictions = self.web()

        self.assertNotEqual(records(predictions), records(archived))

    def test_an_old_archived_forecast_is_not_restored(self):
        archived = self.web()
        self.restart(after=timedelta(hours=2))

        predictions = self.web()

        self.assertNotEqual(records(predictions), records(archived))

    def test_a_slow_archive_read_reuses_a_prediction_made_meanwhile(self):
        """Request B's archive read outlasts request A's whole prediction."""
        lookup = self.scout.data_archive.recent_open_forecast
        a_done = threading.Event()
        b_reading = threading.Event()

        def slow_for_b(*args, **kwargs):
            if threading.current_thread().name == "B":
                b_reading.set()
                a_done.wait(10)
            return lookup(*args, **kwargs)

        results = {}

        def request(name):
            results[name] = self.web()
            if name == "A":
                a_done.set()

        with patch.object(
            self.scout.data_archive, "recent_open_forecast", side_effect=slow_for_b
        ), self.predictions_made() as predicted:
            b = threading.Thread(target=request, args=("B",), name="B")
            b.start()
            b_reading.wait(5)
            a = threading.Thread(target=request, args=("A",), name="A")
            a.start()
            a.join(30)
            b.join(30)

        self.assertEqual(predicted.call_count, 1)
        self.assertEqual(records(results["B"]), records(results["A"]))
        self.assertFalse(results["B"].attrs["refresh_due"])


if __name__ == "__main__":
    unittest.main()

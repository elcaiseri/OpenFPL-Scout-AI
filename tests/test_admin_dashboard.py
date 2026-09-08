import asyncio
import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pandas as pd

import main
from src.admin_dashboard import AdminDashboard, RuntimeMonitor
from src.data_archive import DataArchive


class OfficialClient:
    def __init__(self):
        self.calls = []
        self.events = [{
            "id": 1, "deadline_time": "2020-08-15T10:00:00Z",
            "finished": True, "data_checked": True, "is_current": True,
        }]
        self.elements = [
            {"id": 1, "stats": {"total_points": 6, "minutes": 180}},
            {"id": 2, "stats": {"total_points": 0, "minutes": 0}},
            {"id": 3, "stats": {"total_points": -2, "minutes": 90}},
        ]

    def bootstrap(self):
        return {"events": self.events}

    def event_live(self, gw, *, refresh=False):
        self.calls.append(gw)
        return {"elements": self.elements}


def forecast_bundle():
    predictions = [
        {"id": i, "web_name": f"Player {i}", "element_type": i, "team_name": "Club", "gameweek": 1, "expected_points": expected}
        for i, expected in [(1, 4), (2, 2), (3, 1), (4, 5)]
    ]
    return {
        "metadata": {
            "season": "2020-2021", "prediction_gameweek": 1,
            "captured_at_utc": "2020-08-14T10:00:00Z",
            "inference": {"successful_models": ["ridge"], "weights": {"ridge": 1}},
        },
        "deadline_time": "2020-08-15T10:00:00Z",
        "predictions": predictions,
        "squad": {
            "season": "2020-2021", "prediction_gameweek": 1,
            "captured_at_utc": "2020-08-14T10:00:01Z",
            "players": [{**p, "role": "captain" if p["id"] == 1 else ""} for p in predictions],
        },
    }


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.client = OfficialClient()
        self.scout = SimpleNamespace(
            data_archive=DataArchive(self.root), official_client=self.client,
            config={"models": {"ridge": {"version": "v1"}}},
            model_artifacts=[SimpleNamespace(name="ridge")], fpl_data_enabled=False,
        )
        self.dashboard = AdminDashboard(self.scout, cache_seconds=0)
        self.bundle = forecast_bundle()
        self.save_bundle()

    def save_bundle(self, season="2020-2021"):
        target = self.root / season / "evaluation/gw_01.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.bundle))

    def test_metrics_match_ids_retain_zero_negative_and_missing_scores(self):
        report = self.dashboard.report()
        self.assertEqual(report["summary"]["count"], 3)
        self.assertAlmostEqual(report["summary"]["mae"], 7 / 3)
        self.assertAlmostEqual(report["summary"]["rmse"], math.sqrt(17 / 3))
        self.assertAlmostEqual(report["summary"]["bias"], 1)
        self.assertAlmostEqual(report["summary"]["within_two_pct"], 200 / 3)
        players = {p["id"]: p for p in report["selected"]["players"]}
        self.assertEqual(players[1]["actual_points"], 6)  # Event total, not doubled for 180 minutes.
        self.assertEqual(players[2]["actual_points"], 0)
        self.assertEqual(players[3]["actual_points"], -2)
        self.assertIsNone(players[4]["actual_points"])
        self.assertIsNone(report["selected"]["squad"]["actual_points"])
        self.assertTrue(report["selected"]["squad"]["eligible"])
        json.dumps(report, allow_nan=False)

    def test_scout_report_exposes_archived_picks_and_season_xpts_comparisons(self):
        report = self.dashboard.report()
        scout = report['selected']['analysis']['scout']
        self.assertEqual(scout['expected_points'], 12)
        self.assertIsNone(scout['actual_points'])
        self.assertEqual(scout['captain']['actual_points'], 6)
        self.assertEqual(scout['metrics']['predicted_total'], 7)
        self.assertEqual(scout['metrics']['actual_total'], 4)
        self.assertEqual(report['analytics']['all']['scout']['metrics'], scout['metrics'])
        self.assertEqual(report['analytics']['verified']['scout']['timeline'][0]['count'], 3)
        self.assertEqual(report['selected']['squad']['captured_at_utc'], self.bundle['squad']['captured_at_utc'])

    def test_legacy_csv_preserves_float_precision_when_matching_json_scout_picks(self):
        expected = 0.30000000000000004
        self.bundle['predictions'][0]['expected_points'] = expected
        self.bundle['squad']['players'][0]['expected_points'] = expected
        root = self.root / '2020-2021'
        (root / 'evaluation/gw_01.json').unlink()
        for folder in ('predictions', 'metadata', 'squads'):
            (root / folder).mkdir()
        pd.DataFrame(self.bundle['predictions']).to_csv(root / 'predictions/gw_01.csv', index=False)
        (root / 'metadata/gw_01.json').write_text(json.dumps(self.bundle['metadata']))
        (root / 'squads/gw_01.json').write_text(json.dumps(self.bundle['squad']))
        report = self.dashboard.report()
        self.assertTrue(report['selected']['squad']['matches_forecast'])
        self.assertEqual(report['selected']['analysis']['scout']['captain']['expected_points'], expected)
        self.assertEqual(report['selected']['analysis']['scout']['count'], 4)

    def test_cold_start_has_actual_returns_and_rankings_but_no_points_error(self):
        self.bundle["metadata"]["inference"]["strategy"] = "ownership-cold-start"
        self.save_bundle()
        report = self.dashboard.report()
        self.assertEqual(report["summary"]["count"], 0)
        self.assertEqual(report["comparison_summary"]["count"], 0)
        self.assertEqual(report["selected"]["matched_actuals"], 3)
        self.assertEqual(report["selected"]["analysis"]["selection"]["pool"], 3)
        self.assertFalse(report["selected"]["is_points_forecast"])
        self.assertTrue(any("ownership" in warning for warning in report["warnings"]))
        analytics = report['analytics']['all']
        self.assertEqual(analytics['actual']['points'], 4)
        self.assertEqual(analytics['actual']['count'], 3)
        self.assertEqual(analytics['actual']['gameweeks'], 1)
        self.assertEqual(analytics['clubs'][0]['actual']['points'], 4)
        self.assertEqual(analytics['players'][0]['id'], 1)
        self.assertEqual(analytics['players'][0]['actual']['points'], 6)
        self.assertEqual(analytics['scout']['actual']['points'], 4)
        self.assertEqual(self.dashboard.player_report(1)['actual']['points'], 6)

    def test_retrospective_snapshot_keeps_original_timing_and_final_results(self):
        self.bundle['metadata']['captured_at_utc'] = '2020-08-16T10:00:00Z'
        self.bundle['metadata']['inference']['strategy'] = 'ownership-cold-start'
        self.bundle['snapshot_kind'] = 'retrospective-import'
        self.bundle['snapshot_created_at_utc'] = '2020-09-01T10:00:00Z'
        self.bundle['actuals_snapshot'] = {
            'season': '2020-2021', 'gameweek': 1, 'finalized': True,
            'fetched_at_utc': '2020-08-16T10:00:00Z',
            'source': 'official-fpl', 'payload': {'elements': self.client.elements},
        }
        self.save_bundle()
        with patch.object(self.client, 'event_live', side_effect=RuntimeError('offline')):
            report = self.dashboard.report()
            self.assertEqual(report['selected']['forecast_state'], 'post-deadline')
            self.assertEqual(report['selected']['result_state'], 'final')
            self.assertEqual(report['selected']['snapshot_kind'], 'retrospective-import')
            self.assertEqual(report['analytics']['all']['actual']['points'], 4)
            self.assertEqual(report['analytics']['verified']['actual']['count'], 0)
            self.assertEqual(report['summary']['count'], 0)
            self.bundle['actuals_snapshot']['season'] = '2019-2020'
            self.save_bundle()
            self.assertEqual(self.dashboard.report()['analytics']['all']['actual']['count'], 0)

    def test_player_dossier_is_season_scoped_and_retains_model_predictions(self):
        self.bundle["model_predictions"] = {"ridge": {"1": 8}}
        self.bundle["player_context"] = {"1": {"now_cost": 55, "selected_by_percent": "3.4"}}
        self.save_bundle()
        report = self.dashboard.player_report(1, "2020-2021")
        self.assertEqual(report["player"]["price"], 5.5)
        self.assertEqual(report["player"]["model_predictions"], {"ridge": 8})
        self.assertEqual(report["metrics"]["actual_total"], 6)
        self.assertEqual(len(report["history"]), 1)
        with self.assertRaises(ValueError):
            self.dashboard.player_report(999, "2020-2021")
        with self.assertRaises(ValueError):
            self.dashboard.player_report(1, "2021-2022")

    def test_football_results_are_available_without_a_saved_forecast(self):
        (self.root / "2020-2021/evaluation/gw_01.json").unlink()
        report = self.dashboard.report()
        self.assertEqual(report["selected"]["prediction_count"], 0)
        self.assertEqual(report["selected"]["official_player_count"], 3)
        self.assertEqual(report["selected"]["result_state"], "final")

    def test_overwritten_squad_is_not_used_for_derived_decisions(self):
        self.bundle["squad"]["players"][0]["expected_points"] = 99
        self.save_bundle()
        report = self.dashboard.report()
        self.assertFalse(report["selected"]["squad"]["matches_forecast"])
        self.assertEqual(report["selected"]["analysis"]["squad"]["xi"], [])
        self.assertEqual(report["analytics"]["verified"]["decisions"], [])

    def test_late_unknown_or_wrong_season_forecasts_are_not_accuracy(self):
        for changes in [
            {"captured_at_utc": "2020-08-15T10:00:00Z"},
            {"captured_at_utc": None},
            {"captured_at_utc": "2020-08-14T10:00:00"},
            {"season": "2019-2020"},
            {"prediction_gameweek": 2},
        ]:
            with self.subTest(changes=changes):
                self.bundle = forecast_bundle()
                self.bundle["metadata"].update(changes)
                self.save_bundle()
                report = self.dashboard.report()
                self.assertEqual(report["summary"]["count"], 0)
                self.assertIsNone(report["summary"]["mae"])
                invalid_identity = "season" in changes or "prediction_gameweek" in changes
                self.assertEqual(report["selected"]["metrics"]["count"], 0 if invalid_identity else 3)
                self.assertEqual(report["comparison_summary"]["count"], 0 if invalid_identity else 3)

    def test_late_runs_populate_comparison_without_becoming_verified_accuracy(self):
        self.bundle["metadata"]["captured_at_utc"] = "2020-08-16T10:00:00Z"
        self.save_bundle()
        report = self.dashboard.report()
        comparison = report["comparison_summary"]
        self.assertEqual(report["summary"]["count"], 0)
        self.assertEqual(report["selected"]["forecast_state"], "post-deadline")
        self.assertEqual(comparison["evaluated_gameweeks"], 1)
        self.assertEqual(comparison["post_deadline_gameweeks"], 1)
        self.assertEqual(comparison["verified_gameweeks"], 0)
        self.assertEqual(comparison["count"], 3)
        self.assertAlmostEqual(comparison["mae"], 7 / 3)
        self.assertAlmostEqual(comparison["rmse"], math.sqrt(17 / 3))

    def test_three_late_runs_and_upcoming_forecast_default_to_latest_scored_week(self):
        self.client.events = []
        for gw in range(1, 5):
            deadline = f"2020-08-{14 + gw}T10:00:00Z" if gw < 4 else "2099-08-18T10:00:00Z"
            self.client.events.append({"id": gw, "deadline_time": deadline, "finished": gw < 4, "data_checked": gw < 4})
            bundle = forecast_bundle()
            bundle["metadata"].update(prediction_gameweek=gw, captured_at_utc="2020-08-19T10:00:00Z")
            bundle["deadline_time"] = deadline
            bundle["squad"] = None
            for player in bundle["predictions"]:
                player["gameweek"] = gw
            target = self.root / "2020-2021/evaluation" / f"gw_{gw:02d}.json"
            target.write_text(json.dumps(bundle))
        report = self.dashboard.report()
        self.assertEqual(report["selected"]["gameweek"], 3)
        self.assertEqual(report["summary"]["evaluated_gameweeks"], 0)
        self.assertEqual(report["comparison_summary"]["count"], 9)
        self.assertEqual(report["comparison_summary"]["evaluated_gameweeks"], 3)
        self.assertEqual(report["comparison_summary"]["post_deadline_gameweeks"], 3)
        self.assertEqual(report["comparison_summary"]["archived_gameweeks"], 4)
        self.assertEqual(self.dashboard.report(gameweek=4)["selected"]["result_state"], "awaiting-results")

    def test_mixed_comparisons_use_weighted_player_metrics(self):
        second = forecast_bundle()
        second["metadata"].update(prediction_gameweek=2, captured_at_utc="2020-08-17T10:00:00Z")
        second["predictions"] = [{**second["predictions"][0], "gameweek": 2, "expected_points": 16}]
        second["squad"] = None
        (self.root / "2020-2021/evaluation/gw_02.json").write_text(json.dumps(second))
        self.client.events.append({"id": 2, "deadline_time": "2020-08-16T10:00:00Z", "finished": True, "data_checked": True})
        report = self.dashboard.report()
        self.assertEqual(report["summary"]["count"], 3)
        self.assertAlmostEqual(report["comparison_summary"]["mae"], 17 / 4)
        self.assertAlmostEqual(report["comparison_summary"]["rmse"], math.sqrt(117 / 4))
        self.assertEqual(report["comparison_summary"]["verified_gameweeks"], 1)
        self.assertEqual(report["comparison_summary"]["post_deadline_gameweeks"], 1)

    def test_provisional_scores_never_count_as_final(self):
        self.client.events[0]["data_checked"] = False
        report = self.dashboard.report()
        self.assertEqual(report["selected"]["result_state"], "provisional")
        self.assertEqual(report["summary"]["count"], 0)
        self.assertEqual(report["comparison_summary"]["count"], 0)
        self.client.events[0]["data_checked"] = True
        self.client.elements[0]["stats"]["total_points"] = 8
        report = self.dashboard.report()
        self.assertEqual(report["summary"]["count"], 3)
        self.assertEqual(self.client.calls, [1, 1])
        self.dashboard.report()
        self.assertEqual(self.client.calls, [1, 1])  # Saved verified final scores are durable.

    def test_duplicates_and_nonfinite_predictions_excluded(self):
        for bad in ["duplicate", "nonfinite", "wrong-gameweek"]:
            self.bundle = forecast_bundle()
            if bad == "duplicate":
                self.bundle["predictions"].append(self.bundle["predictions"][0])
            elif bad == "nonfinite":
                self.bundle["predictions"][0]["expected_points"] = float("inf")
            else:
                self.bundle["predictions"][0]["gameweek"] = 2
            self.save_bundle()
            report = self.dashboard.report()
            self.assertEqual(report["summary"]["count"], 0)
            self.assertTrue(report["warnings"])

    def test_squad_must_match_snapshot_and_be_saved_before_deadline(self):
        self.bundle["squad"]["captured_at_utc"] = "2020-08-16T00:00:00Z"
        self.save_bundle()
        self.assertFalse(self.dashboard.report()["selected"]["squad"]["eligible"])
        self.bundle = forecast_bundle()
        self.bundle["squad"]["players"][0]["expected_points"] = 99
        self.save_bundle()
        self.assertFalse(self.dashboard.report()["selected"]["squad"]["eligible"])

    def test_current_player_ids_are_never_used_for_a_previous_season(self):
        self.client.events[0]["deadline_time"] = "2021-08-15T10:00:00Z"
        report = self.dashboard.report("2020-2021")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(report["summary"]["count"], 0)

    def test_official_outage_uses_saved_scores_and_reports_staleness(self):
        self.client.events[0]["data_checked"] = False
        self.dashboard.report()
        with patch.object(self.client, "event_live", side_effect=RuntimeError("offline")):
            report = self.dashboard.report()
        self.assertTrue(report["warnings"])
        self.assertEqual(report["selected"]["metrics"]["count"], 3)
        self.assertEqual(report["summary"]["count"], 0)

    def test_empty_archive_and_invalid_season(self):
        (self.root / "2020-2021/evaluation/gw_01.json").unlink()
        report = self.dashboard.report()
        self.assertEqual(report["summary"]["archived_gameweeks"], 0)
        self.assertEqual(report["selected"]["forecast_state"], "missing")
        for season in ["../../private", "2020-2021/..", "1990-1991"]:
            with self.assertRaises(ValueError):
                self.dashboard.report(season)

    def test_legacy_predictions_remain_visible_with_timing_checked(self):
        season = self.root / "2020-2021"
        (season / "evaluation/gw_01.json").unlink()
        (season / "predictions").mkdir()
        (season / "metadata").mkdir()
        pd.DataFrame(self.bundle["predictions"]).to_csv(season / "predictions/gw_01.csv", index=False)
        (season / "metadata/gw_01.json").write_text(json.dumps(self.bundle["metadata"]))
        report = self.dashboard.report()
        self.assertTrue(report["selected"]["eligible"])
        self.assertFalse(report["selected"]["preserved"])
        self.assertEqual(report["system"]["models"][0]["last_inference"], "succeeded")


class SnapshotTests(unittest.TestCase):
    def test_latest_pre_deadline_bundle_survives_post_deadline_reruns(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = DataArchive(Path(directory))
            official = OfficialClient()
            official.events[0].update(finished=False, data_checked=False, is_current=False)
            official.fixtures = lambda: []
            official.player_history = lambda *args, **kwargs: pd.DataFrame()
            predictions = pd.DataFrame(forecast_bundle()["predictions"])
            predictions.attrs["gameweek"] = 1
            predictions.attrs["model_predictions"] = {"ridge": {"1": 8.25}}
            args = dict(official_client=official, prediction_gameweek=1, official_history=pd.DataFrame(), enriched_history=pd.DataFrame(), predictions=predictions, source="official-fpl", enrichment={}, model_versions={})
            class FrozenDateTime(datetime):
                current = datetime(2020, 8, 14, tzinfo=timezone.utc)

                @classmethod
                def now(cls, tz=None):
                    return cls.current

            with patch("src.data_archive.datetime", FrozenDateTime):
                archive.capture_inference(**args)
                archive.capture_squad(predictions, predictions)
                predictions.loc[0, "expected_points"] = 7
                FrozenDateTime.current = datetime(2020, 8, 14, 20, tzinfo=timezone.utc)
                archive.capture_inference(**args)
                archive.capture_squad(predictions, predictions)
                target = Path(directory) / "2020-2021/evaluation/gw_01.json"
                before = target.read_bytes()
                saved = json.loads(before)
                self.assertEqual(saved["predictions"][0]["expected_points"], 7)
                self.assertIsNotNone(saved["squad"])
                self.assertEqual(saved["model_predictions"]["ridge"]["1"], 8.25)
                predictions.attrs["model_predictions"] = {"ridge": {"1": 100}}
                predictions.loc[0, "expected_points"] = 99
                FrozenDateTime.current = datetime(2020, 8, 16, tzinfo=timezone.utc)
                archive.capture_inference(**args)
                archive.capture_squad(predictions, predictions)
                self.assertEqual(target.read_bytes(), before)


async def asgi_get(path, key=None, method="GET"):
    """Exercise real routing, dependencies and middleware without a test client dependency."""
    parts = urlsplit(path)
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await main.app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http", "path": parts.path,
        "raw_path": parts.path.encode(), "query_string": parts.query.encode(), "root_path": "",
        "headers": [(b"authorization", f"Bearer {key}".encode())] if key else [],
        "client": ("127.0.0.1", 123), "server": ("test", 80),
    }, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_owner_routes_fail_closed_and_stay_out_of_public_schema(self):
        paths = [("/api/admin/players/1", "GET"), ("/api/admin/models", "GET"), ("/api/admin/capture?gameweek=4", "POST")]
        for path, method in paths:
            with patch.dict("os.environ", {"OPENFPL_ADMIN_KEY": "owner-test-key"}):
                status, headers, _ = await asgi_get(path, "shared-api-key", method)
                self.assertEqual(status, 401)
                self.assertEqual(headers[b"cache-control"], b"no-store")
            with patch.dict("os.environ", {"OPENFPL_ADMIN_KEY": ""}):
                status, _, _ = await asgi_get(path, "owner-test-key", method)
                self.assertEqual(status, 503)
        self.assertFalse(any(path.startswith("/api/admin") for path in main.app.openapi()["paths"]))

    async def test_capture_rejects_past_deadline_and_new_queries_are_validated(self):
        fake = SimpleNamespace(official_client=OfficialClient())
        with patch.dict("os.environ", {"OPENFPL_ADMIN_KEY": "owner-test-key"}), patch.object(main, "scout", fake, create=True):
            status, _, _ = await asgi_get("/api/admin/capture?gameweek=1", "owner-test-key", "POST")
            self.assertEqual(status, 422)
            for path in ["/api/admin/models?dataset=private", "/api/admin/players/0", "/api/admin/players/1?season=private"]:
                status, _, _ = await asgi_get(path, "owner-test-key")
                self.assertEqual(status, 422)

    async def test_owner_endpoint_fails_closed_and_does_not_accept_shared_keys(self):
        with patch.dict("os.environ", {"OPENFPL_ADMIN_KEY": "owner-test-key"}):
            for path, key in [("/api/admin/dashboard", None), ("/api/admin/dashboard", "shared-api-key"), ("/api/admin/dashboard?token=owner-test-key", None)]:
                status, headers, body = await asgi_get(path, key)
                self.assertEqual(status, 401)
                self.assertEqual(headers[b"cache-control"], b"no-store")
                self.assertEqual(headers[b"x-robots-tag"], b"noindex, nofollow")
        with patch.dict("os.environ", {"OPENFPL_ADMIN_KEY": ""}):
            status, _, _ = await asgi_get("/api/admin/dashboard", "anything")
            self.assertEqual(status, 503)

    async def test_authorized_access_and_query_validation(self):
        fake = SimpleNamespace(report=lambda season, gameweek: {"selected": {"secret": "owner-data"}})
        with patch.dict("os.environ", {"OPENFPL_ADMIN_KEY": "owner-test-key"}), patch.object(main, "admin_dashboard", fake, create=True):
            status, headers, body = await asgi_get("/api/admin/dashboard", "owner-test-key")
            self.assertEqual(status, 200)
            self.assertIn(b"owner-data", body)
            self.assertEqual(headers[b"cache-control"], b"no-store")
            for query in ["gameweek=39", "season=../../private"]:
                status, _, _ = await asgi_get(f"/api/admin/dashboard?{query}", "owner-test-key")
                self.assertEqual(status, 422)

    async def test_login_shell_is_unindexed_data_free_and_no_store(self):
        status, headers, body = await asgi_get("/admin")
        self.assertEqual(status, 200)
        self.assertIn(b"Owner access key", body)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertIn(b"frame-ancestors 'none'", headers[b"content-security-policy"])
        self.assertNotIn("/api/admin/dashboard", main.app.openapi()["paths"])
        self.assertNotIn("/admin", main.SITEMAP_PATHS)

    def test_runtime_monitor_is_bounded(self):
        monitor = RuntimeMonitor()
        for i in range(250):
            monitor.record(500 if i == 0 else 200, i / 1000)
        result = monitor.snapshot()
        self.assertEqual(result["requests"], 250)
        self.assertEqual(result["server_errors"], 1)
        self.assertEqual(result["latency_sample_size"], 200)
        self.assertAlmostEqual(result["p95_latency_ms"], 239)


if __name__ == "__main__":
    unittest.main()

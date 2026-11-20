"""The private owner dashboard: hidden, token-gated, read-only unless asked."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import main
from main import _api_catalog, _uses_bearer_auth, app
from src.data_archive import DataArchive
from src.features import MODEL_FEATURES
from src.official_fpl import OfficialFPLClient
from src.scout import FPLScout
from tests.test_frozen_forecasts import BEFORE_GW3_DEADLINE, FakeFPL

TOKEN = "dashboard-token"


class GoalsModel:
    feature_names_in_ = np.asarray(MODEL_FEATURES)

    def __init__(self, scale=3.0):
        self.scale = scale

    def predict(self, features):
        return 2.0 + self.scale * features["goals"].fillna(0).to_numpy()


def request(method, target, *, token=TOKEN, body=None):
    """One request through the whole ASGI app (middleware, auth, routes)."""
    path, _, query = target.partition("?")
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    payload = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(item for item in messages if item["type"] == "http.response.start")
    raw = b"".join(
        item.get("body", b"") for item in messages if item["type"] == "http.response.body"
    )
    response_headers = {name.decode().lower(): value.decode() for name, value in start["headers"]}
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = raw.decode()
    return start["status"], response_headers, data


def admin_routes():
    return [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/admin")
    ]


class AdminRouteStructureTests(unittest.TestCase):
    def test_every_admin_route_requires_a_bearer_token(self):
        routes = admin_routes()
        self.assertGreaterEqual(len(routes), 18)
        for route in routes:
            self.assertTrue(_uses_bearer_auth(route.dependant), route.path)

    def test_admin_routes_stay_out_of_public_docs_and_catalog(self):
        schema = app.openapi()
        self.assertFalse([path for path in schema["paths"] if path.startswith("/api/admin")])
        catalog_paths = [
            endpoint["path"] for group in _api_catalog() for endpoint in group["endpoints"]
        ]
        self.assertFalse([path for path in catalog_paths if path.startswith("/api/admin")])
        self.assertNotIn("/admin", schema["paths"])

    def test_requests_without_a_valid_token_are_rejected(self):
        with patch("src.auth.VALID_API_KEYS", {TOKEN}):
            self.assertIn(request("GET", "/api/admin/overview", token=None)[0], (401, 403))
            self.assertEqual(request("GET", "/api/admin/overview", token="wrong")[0], 401)


class AdminPageTests(unittest.TestCase):
    def test_page_is_noindex_no_cache_and_versions_its_assets(self):
        status, headers, body = request("GET", "/admin", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(headers["x-robots-tag"], "noindex, nofollow")
        self.assertIn('/static/admin/admin.js?v=', body)
        self.assertIn('/static/admin/admin.css?v=', body)
        self.assertIn('name="robots" content="noindex, nofollow"', body)

    def test_head_is_answered(self):
        self.assertEqual(request("HEAD", "/admin", token=None)[0], 200)

    def test_kill_switch_hides_page_and_api(self):
        with patch.dict(os.environ, {"OPENFPL_ADMIN_ENABLED": "false"}), patch(
            "src.auth.VALID_API_KEYS", {TOKEN}
        ):
            self.assertEqual(request("GET", "/admin", token=None)[0], 404)
            self.assertEqual(request("GET", "/api/admin/overview")[0], 404)


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = [BEFORE_GW3_DEADLINE]
        self.fpl = FakeFPL()
        self.client = OfficialFPLClient(session=self.fpl)
        self.archive = DataArchive(
            Path(directory.name), configured_season="2026-2027", clock=lambda: self.now[0]
        )
        models = {"goals": GoalsModel(3.0), "steady": GoalsModel(2.0), "bold": GoalsModel(4.0)}
        self.scout = FPLScout(
            {
                "inference": {"minimum_successful_models": 2},
                "fpl_data_inference": {"enabled": False},
                "models": {name: {"path": name, "version": "test"} for name in models},
            },
            model_loader=models.__getitem__,
            official_client=self.client,
            data_archive=self.archive,
        )
        for patcher in (
            patch("main.scout", self.scout, create=True),
            patch("src.auth.VALID_API_KEYS", {TOKEN}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def get(self, target):
        status, headers, data = request("GET", target)
        self.assertEqual(status, 200, data)
        self.assertEqual(headers["cache-control"], "no-store")
        return data

    def test_overview_describes_the_service(self):
        data = self.get("/api/admin/overview")
        self.assertEqual(data["models"]["loaded"], 3)
        self.assertEqual(data["gameweeks"]["open_gameweek"], 3)
        self.assertIn("uptime_seconds", data["process"])
        self.assertIn("total_requests", data["requests"])

    def test_predictions_explain_and_squad_agree(self):
        predictions = self.get("/api/admin/predictions?gameweek=3")
        self.assertEqual(predictions["gameweek"], 3)
        self.assertFalse(predictions["frozen"])
        self.assertEqual(predictions["models"], ["goals", "steady", "bold"])
        players = predictions["players"]
        self.assertEqual(sum(row["in_squad"] for row in players), 15)
        top = players[0]
        self.assertEqual(set(top["models"]), {"goals", "steady", "bold"})
        self.assertIn("goals", top["features"])

        explanation = self.get(f"/api/admin/players/{top['id']}/explain?gameweek=3")
        self.assertAlmostEqual(explanation["expected_points"], top["expected_points"], places=2)
        self.assertEqual(explanation["contributions"][0]["feature"], "goals")
        self.assertTrue(explanation["reasons"])

        squad = self.get("/api/admin/squad?gameweek=3")
        self.assertEqual(len(squad["squad"]), 15)
        self.assertEqual(
            {row["id"] for row in squad["squad"]},
            {row["id"] for row in players if row["in_squad"]},
        )
        self.assertEqual(len(squad["lineup"]["starting_ids"]), 11)
        self.assertTrue(all(row["decision_reason"] for row in squad["squad"]))

    def test_models_metrics_archive_config_timeline_and_logs(self):
        self.get("/api/admin/predictions?gameweek=3")
        models = self.get("/api/admin/models?gameweek=3")
        self.assertEqual([card["name"] for card in models["models"]], ["goals", "steady", "bold"])
        self.assertTrue(all(card["last_run"]["status"] == "ok" for card in models["models"]))
        self.assertTrue(models["agreement"]["available"])
        self.assertIn("goals", models["feature_coverage"]["populated"])

        metrics = self.get("/api/admin/metrics")
        self.assertGreater(metrics["upstream"]["requests"], 0)
        self.assertEqual(metrics["model_inputs"][0]["gameweek"], 3)
        self.assertTrue(any("/api/admin/models" in key for key in metrics["requests"]["routes"]))

        archive = self.get("/api/admin/archive")
        self.assertEqual(archive["season"], "2026-2027")
        self.assertEqual(archive["gameweeks"][-1]["gameweek"], 3)

        config = self.get("/api/admin/config")
        self.assertEqual(config["api_keys_configured"], 1)
        self.assertNotIn(TOKEN, json.dumps(config))

        timeline = self.get("/api/admin/timeline")
        self.assertEqual(timeline["events"][-1]["type"], "prediction")
        self.assertIn("records", self.get("/api/admin/logs?level=INFO&limit=5"))

    def test_simulation_is_a_sandbox(self):
        self.get("/api/admin/predictions?gameweek=3")
        cached = self.scout._live_forecasts[3]
        live_squad = self.get("/api/admin/squad?gameweek=3")["squad"]
        captain = live_squad[0]["id"]

        status, _, result = request(
            "POST",
            "/api/admin/simulate",
            body={"gameweek": 3, "weights": {"bold": 0}, "exclude_ids": [captain]},
        )

        self.assertEqual(status, 200, result)
        self.assertTrue(result["sandbox"])
        self.assertNotIn(captain, [row["id"] for row in result["squad"]])
        self.assertIn(captain, [row["id"] for row in result["diff"]["out"]])
        self.assertEqual(set(result["weights"]), {"goals", "steady"})
        self.assertIs(self.scout._live_forecasts[3], cached)
        self.assertEqual(self.scout.weight_overrides, {})

    def test_live_controls_change_the_service_and_are_audited(self):
        self.get("/api/admin/predictions?gameweek=3")
        status, _, data = request("POST", "/api/admin/models/bold", body={"weight": 2.5})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.scout.weight_overrides, {"bold": 2.5})
        self.assertNotIn(3, self.scout._live_forecasts)

        status, _, data = request("POST", "/api/admin/models/goals", body={"enabled": False})
        self.assertEqual(status, 200, data)
        status, _, data = request("POST", "/api/admin/models/steady", body={"enabled": False})
        self.assertEqual(status, 409, data)
        self.assertEqual(request("POST", "/api/admin/models/nope", body={"weight": 1})[0], 404)

        status, _, data = request("POST", "/api/admin/predictions/recompute?gameweek=3")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["trigger"], "manual")
        self.assertEqual(data["event"]["disabled_models"], ["goals"])
        self.assertEqual(data["forecast"]["weights"], {"steady": 1.0, "bold": 2.5})

        self.assertEqual(request("POST", "/api/admin/models/reset")[0], 200)
        self.assertEqual((self.scout.weight_overrides, self.scout.disabled_models), ({}, set()))

        status, _, data = request("POST", "/api/admin/cache/clear", body={})
        self.assertEqual(status, 200, data)
        self.assertGreater(data["official_entries_cleared"], 0)

        status, _, data = request("POST", "/api/admin/archive/collect?force=true")
        self.assertEqual((status, data["status"]), (200, "saved"))

        actions = [
            event["action"] for event in self.scout.forecast_events if event["type"] == "admin"
        ]
        self.assertEqual(
            actions,
            ["model-override", "model-override", "recompute", "reset-model-overrides", "clear-cache", "collect-results"],
        )

    def test_a_frozen_gameweek_cannot_be_recomputed(self):
        self.fpl.finish(3)
        self.now[0] = self.now[0].replace(day=30)
        status, _, data = request("POST", "/api/admin/predictions/recompute?gameweek=3")
        self.assertEqual(status, 409, data)
        self.assertIn("frozen", data["detail"])

    def test_enrichment_toggle_builds_the_provider_lazily(self):
        self.assertIsNone(self.scout.fpl_data_provider)
        with patch.object(self.scout, "_build_fpl_data_provider", return_value=object()) as build:
            status, _, data = request("POST", "/api/admin/enrichment", body={"enabled": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(self.scout.fpl_data_enabled)
        build.assert_called_once()
        self.assertEqual(request("POST", "/api/admin/enrichment", body={"enabled": False})[0], 200)
        self.assertFalse(self.scout.fpl_data_enabled)

    def test_accuracy_without_archived_finished_forecasts_lists_them_missing(self):
        data = self.get("/api/admin/accuracy")
        self.assertTrue(data["archive_enabled"])
        self.assertEqual(data["gameweeks"], [])
        self.assertEqual(data["missing"], [1, 2])


if __name__ == "__main__":
    unittest.main()

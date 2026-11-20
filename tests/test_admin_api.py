"""The private owner dashboard: hidden, token-gated, read-only unless asked."""

import asyncio
import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import numpy as np

import main
from main import _api_catalog, _uses_bearer_auth, app
from src.data_archive import DataArchive
from src.features import MODEL_FEATURES
from src.official_fpl import OfficialFPLClient
from src.scout import FPLScout
from tests.test_frozen_forecasts import BEFORE_GW3_DEADLINE, FakeFPL, FakeResponse

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




class ManagerFPL(FakeFPL):
    """FakeFPL plus one public manager (entry 77) who picked a legal squad."""

    ENTRY = 77

    def __init__(self):
        super().__init__()
        clubs = Counter()
        quotas = {1: 2, 2: 5, 3: 5, 4: 3}
        self.squad = []
        for player in self.elements:
            position = player["element_type"]
            if quotas[position] and clubs[player["team"]] < 3:
                self.squad.append(player["id"])
                quotas[position] -= 1
                clubs[player["team"]] += 1
        # A legal lineup: 1-4-4-2 starting, then GK, DEF, MID, FWD on the bench.
        by_position = {
            position: [pid for pid in self.squad if self.elements[pid - 1]["element_type"] == position]
            for position in (1, 2, 3, 4)
        }
        self.squad = (
            by_position[1][:1] + by_position[2][:4] + by_position[3][:4] + by_position[4][:2]
            + by_position[1][1:] + by_position[2][4:] + by_position[3][4:] + by_position[4][2:]
        )
        self.bought = by_position[2][0]

    def manager_payloads(self):
        entry = self.ENTRY
        return {
            f"entry/{entry}/": {
                "id": entry, "name": "Test XI", "player_first_name": "Test",
                "player_last_name": "Manager", "current_event": 2, "started_event": 1,
                "last_deadline_bank": 15, "last_deadline_value": 750, "favourite_team": None,
                "leagues": {"classic": [], "h2h": []},
            },
            f"entry/{entry}/history/": {
                "current": [
                    {"event": 1, "event_transfers": 0, "event_transfers_cost": 0},
                    {"event": 2, "event_transfers": 1, "event_transfers_cost": 0},
                ],
                "chips": [], "past": [],
            },
            f"entry/{entry}/transfers/": [
                {"element_in": self.bought, "element_in_cost": 45, "element_out": 90,
                 "element_out_cost": 50, "event": 2, "time": "2026-08-21T10:00:00Z"},
            ],
            f"entry/{entry}/event/2/picks/": {
                "active_chip": None, "automatic_subs": [], "entry_history": {"event": 2, "bank": 15},
                "picks": [
                    {"element": player_id, "position": index + 1, "multiplier": 2 if index == 0 else 1 if index < 11 else 0,
                     "is_captain": index == 0, "is_vice_captain": index == 1}
                    for index, player_id in enumerate(self.squad)
                ],
            },
        }

    def get(self, url, timeout):
        path = url.split("/api/", 1)[1]
        payloads = self.manager_payloads()
        if path in payloads:
            return FakeResponse(json.loads(json.dumps(payloads[path])))
        return super().get(url, timeout)


class OptimizerApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = [BEFORE_GW3_DEADLINE]
        self.fpl = ManagerFPL()
        self.client = OfficialFPLClient(session=self.fpl)
        archive = DataArchive(Path(directory.name), configured_season="2026-2027", clock=lambda: self.now[0])
        models = {"goals": GoalsModel(3.0), "steady": GoalsModel(2.0)}
        self.scout = FPLScout(
            {
                "inference": {"minimum_successful_models": 1},
                "fpl_data_inference": {"enabled": False},
                "models": {name: {"path": name} for name in models},
            },
            model_loader=models.__getitem__,
            official_client=self.client,
            data_archive=archive,
        )
        for patcher in (
            patch("main.scout", self.scout, create=True),
            patch("src.auth.VALID_API_KEYS", {TOKEN}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plan_routes_are_private(self):
        for path in ("/api/admin/managers/{entry_id}/plan-context", "/api/admin/optimize"):
            route = next(item for item in admin_routes() if item.path == path)
            self.assertTrue(_uses_bearer_auth(route.dependant))
        self.assertNotIn("/api/admin/optimize", app.openapi()["paths"])

    def test_plan_context_estimates_transfers_prices_and_budget(self):
        status, _, context = request("GET", "/api/admin/managers/77/plan-context?gameweek=3")
        self.assertEqual(status, 200, context)
        self.assertEqual(context["picks_gameweek"], 2)
        self.assertEqual(context["free_transfers"]["free_transfers"], 1)
        self.assertEqual(context["bank"], 1.5)
        bought = next(row for row in context["squad"] if row["id"] == self.fpl.bought)
        # Bought at 4.5, now 5.0: sells for 4.5 + half of the 0.5 rise, rounded down.
        self.assertEqual((bought["purchase_price"], bought["selling_price"]), (4.5, 4.7))
        self.assertAlmostEqual(context["budget"], 1.5 + 14 * 5.0 + 4.7, places=6)
        self.assertEqual(len(context["squad"]), 15)

    def test_every_mode_returns_a_legal_plan(self):
        for mode, horizon in (("transfers", 2), ("freehit", None), ("wildcard", None)):
            body = {"entry_id": 77, "gameweek": 3, "mode": mode}
            if horizon:
                body.update(horizon=horizon, max_transfers=2)
            status, _, result = request("POST", "/api/admin/optimize", body=body)
            self.assertEqual(status, 200, (mode, result))
            expected = {"transfers": [3, 4], "freehit": [3], "wildcard": [3, 4, 5, 6, 7]}[mode]
            self.assertEqual(result["gameweeks"], expected)
            plan = result["plans"][result["best_index"]]
            squad = plan["squad"]
            self.assertEqual(Counter(player["position"] for player in squad), Counter({"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}))
            self.assertLessEqual(max(Counter(player["team_name"] for player in squad).values()), 3)
            self.assertLessEqual(plan["spent"], result["budget"] + 1e-9)
            if mode == "transfers":
                self.assertEqual([item["transfers"] for item in result["plans"]], [0, 1, 2])
                self.assertEqual(result["plans"][2]["hits"], 1)
            if mode == "wildcard":
                # FakeFPL publishes fixtures up to GW4 only.
                self.assertTrue(any("GW5" in note for note in result["notes"]))

    def test_closed_gameweeks_cannot_be_planned(self):
        self.now[0] = self.now[0].replace(day=30)
        status, _, data = request("GET", "/api/admin/managers/77/plan-context?gameweek=3")
        self.assertEqual(status, 409, data)
        status, _, data = request("POST", "/api/admin/optimize", body={"entry_id": 77, "gameweek": 3})
        self.assertEqual(status, 409, data)


class WorkingTeamApiTests(OptimizerApiTests):
    """The working squad: a manager's edits, overrides and the advice on them."""

    def setUp(self):
        super().setUp()
        published = self.fpl.manager_payloads()["entry/77/event/2/picks/"]["picks"]
        self.published = [
            {"id": pick["element"], "position": pick["position"], "is_captain": pick["is_captain"],
             "is_vice_captain": pick["is_vice_captain"]}
            for pick in published
        ]
        self.elements = {player["id"]: player for player in self.fpl.elements}

    # Inherited tests already run in OptimizerApiTests.
    def test_plan_routes_are_private(self):
        pass

    def test_plan_context_estimates_transfers_prices_and_budget(self):
        pass

    def test_every_mode_returns_a_legal_plan(self):
        pass

    def test_closed_gameweeks_cannot_be_planned(self):
        pass

    def team(self, **body):
        body.setdefault("gameweek", 3)
        return request("POST", "/api/admin/managers/77/team", body=body)

    def swap_options(self, position, skip=()):
        """A squad player of ``position`` and an outsider who may replace him."""
        squad = [pick["id"] for pick in self.published]
        clubs = Counter(self.elements[pid]["team"] for pid in squad)
        out_id = next(pid for pid in squad if self.elements[pid]["element_type"] == position
                      and pid != self.fpl.bought and pid not in skip)
        clubs[self.elements[out_id]["team"]] -= 1
        in_id = next(player["id"] for player in self.fpl.elements
                     if player["element_type"] == position and player["id"] not in squad
                     and player["id"] not in skip and clubs[player["team"]] < 3)
        return out_id, in_id

    def draft(self, swaps=(), **extra):
        picks = [dict(pick) for pick in self.published]
        for out_id, in_id in swaps:
            for pick in picks:
                if pick["id"] == out_id:
                    pick["id"] = in_id
        return {"picks": picks, **extra}

    def test_working_team_route_is_private(self):
        route = next(item for item in admin_routes() if item.path == "/api/admin/managers/{entry_id}/team")
        self.assertTrue(_uses_bearer_auth(route.dependant))
        self.assertNotIn("/api/admin/managers/{entry_id}/team", app.openapi()["paths"])
        status, _, _ = request("POST", "/api/admin/managers/77/team", token=None, body={})
        self.assertIn(status, (401, 403))

    def test_without_edits_the_working_rating_matches_the_public_rating(self):
        status, _, data = self.team()
        self.assertEqual(status, 200, data)
        status, _, public = request("GET", "/api/scout/team-rating?entry_id=77&gameweek=3")
        self.assertEqual(status, 200, public)
        for key in ("rating", "grade", "components", "projected_points", "ai_projected_points"):
            self.assertEqual(data["rating"][key], public[key], key)
            self.assertEqual(data["published_rating"][key], public[key], key)
        self.assertFalse(data["edited"])
        self.assertTrue(data["validation"]["valid"], data["validation"])
        self.assertEqual(data["free_transfers"]["free_transfers"], 1)
        self.assertEqual(len(data["squad"]), 15)
        self.assertIsNotNone(data["expected"])
        self.assertTrue(data["advice"]["reasons"])
        self.assertGreaterEqual(data["advice"]["gain"], 0)
        self.assertTrue(data["captaincy"])
        row = data["squad"][0]
        for key in ("price_pressure", "selling_effect", "fixtures", "transfers_in_event"):
            self.assertIn(key, row)

    def test_a_draft_transfer_spends_the_bank_and_extra_ones_cost_hits(self):
        first = self.swap_options(4)
        status, _, data = self.team(draft=self.draft([first]))
        self.assertEqual(status, 200, data)
        self.assertTrue(data["edited"])
        self.assertTrue(data["validation"]["valid"], data["validation"])
        changes = data["changes"]
        self.assertEqual((changes["count"], changes["hits"], changes["free_transfers_remaining"]), (1, 0, 0))
        # Sold at 5.0 (bought at 5.0 in GW1), bought at 5.0.
        self.assertEqual(data["bank"], 1.5)
        incoming = next(row for row in data["squad"] if row["id"] == first[1])
        self.assertTrue(incoming["new"])
        self.assertNotEqual(data["rating"], None)

        second = self.swap_options(3, skip=first)
        status, _, data = self.team(draft=self.draft([first, second]))
        self.assertEqual(status, 200, data)
        self.assertEqual((data["changes"]["count"], data["changes"]["hits"]), (2, 1))
        status, _, data = self.team(draft=self.draft([first, second], chip="wildcard"))
        self.assertEqual(data["changes"]["hits"], 0)
        status, _, data = self.team(draft=self.draft([first], bank_override=0.2, free_transfers=0))
        self.assertEqual((data["bank"], data["changes"]["hits"]), (0.2, 1))

    def test_an_illegal_draft_is_reported_not_rejected(self):
        picks = self.draft()
        for pick in picks["picks"]:
            pick["is_captain"] = pick["position"] == 13
        status, _, data = self.team(draft=picks)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["validation"]["valid"])
        self.assertTrue(any("captain" in error for error in data["validation"]["errors"]))
        self.assertEqual(data["transfers"], [])

    def test_drafts_need_an_open_gameweek(self):
        self.now[0] = self.now[0].replace(day=30)
        status, _, data = self.team(draft=self.draft())
        self.assertEqual(status, 409, data)
        status, _, data = self.team()
        self.assertEqual(status, 200, data)
        self.assertFalse(data["transfers_open"])
        self.assertEqual(data["transfers"], [])
        self.assertIsNone(data["free_transfers"])

    def test_overrides_change_the_working_view_only(self):
        captain = next(pick["id"] for pick in self.published if pick["is_captain"])
        status, _, plain = self.team()
        status, _, data = self.team(overrides={str(captain): {"xp_pct": 0}})
        self.assertEqual(status, 200, data)
        self.assertLess(data["rating"]["projected_points"], plain["rating"]["projected_points"])
        self.assertEqual(data["published_rating"], plain["published_rating"])
        self.assertEqual(data["overrides_applied"], [captain])
        status, _, data = self.team(overrides={str(captain): {"lock": True, "ban": True}})
        self.assertEqual(status, 422, data)

    def test_a_player_ruled_out_can_be_re_projected_as_fit(self):
        injured = next(pick["id"] for pick in self.published if pick["position"] == 5)
        self.fpl.elements[injured - 1].update(status="i", chance_of_playing_next_round=0)
        status, _, plain = self.team()
        self.assertEqual(status, 200, plain)
        row = next(item for item in plain["squad"] if item["id"] == injured)
        self.assertEqual(row["expected_points"], 0.0)
        status, _, data = self.team(overrides={str(injured): {"availability": 100}})
        self.assertEqual(status, 200, data)
        row = next(item for item in data["squad"] if item["id"] == injured)
        self.assertGreater(row["expected_points"], 0.0)
        self.assertEqual(row["availability_factor"], 1.0)
        self.assertTrue(row["overridden"])

    def test_optimize_plans_from_the_working_squad_with_locks_and_bans(self):
        first = self.swap_options(4)
        draft = self.draft([first])
        locked_out, locked_in = self.swap_options(3, skip=first)
        body = {
            "entry_id": 77, "gameweek": 3, "mode": "transfers", "horizon": 1, "max_transfers": 2,
            "draft": draft, "free_transfers": 1,
            "overrides": {str(locked_in): {"lock": True}, str(locked_out): {"ban": True}},
        }
        status, _, result = request("POST", "/api/admin/optimize", body=body)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["from_working_squad"])
        self.assertEqual(sorted(result["plans"][0]["squad_ids"]), sorted(pick["id"] for pick in draft["picks"]))
        self.assertEqual(result["locked_ids"], [locked_in])
        for plan in result["plans"][1:]:
            if plan["feasible"]:
                self.assertIn(locked_in, plan["squad_ids"])
                self.assertNotIn(locked_out, plan["squad_ids"])
        self.assertTrue(result["plans"][1]["feasible"])


if __name__ == "__main__":
    unittest.main()

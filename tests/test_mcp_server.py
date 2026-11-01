"""The MCP endpoint at /mcp, driven over HTTP through the real FastAPI app."""

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx2
import pandas as pd

import main
import src.auth
from main import _api_catalog
from src.auth import APIKeyError, bearer_token, check_api_key
from src.official_fpl import OfficialFPLNotFoundError
from tests.test_api_schema import FakeRatingOfficialClient

API_KEY = "test-key"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

# Each tool and the REST route whose handler it calls. Tool tiers must match.
TOOL_ROUTES = {
    "get_scout_squad": ("GET", "/api/scout"),
    "rate_team": ("GET", "/api/scout/team-rating"),
    "get_gameweeks": ("GET", "/api/fpl/gameweeks"),
    "get_fixtures": ("GET", "/api/fpl/fixtures"),
    "get_player_predictions": ("GET", "/api/gw/playerpoints"),
    "get_live_points": ("GET", "/api/fpl/gameweeks/{gameweek}/live"),
    "get_set_piece_notes": ("GET", "/api/fpl/set-piece-notes"),
    "get_manager_picks": (
        "GET",
        "/api/fpl/managers/{entry_id}/gameweeks/{gameweek}/picks",
    ),
}
AUTHENTICATED_TOOLS = {
    "get_player_predictions",
    "get_live_points",
    "get_set_piece_notes",
    "get_manager_picks",
}
AUTHENTICATED_CALLS = {
    "get_player_predictions": {"gameweek": 1},
    "get_live_points": {"gameweek": 1},
    "get_set_piece_notes": {},
    "get_manager_picks": {"entry_id": 7, "gameweek": 1},
}


class FakeOfficialClient(FakeRatingOfficialClient):
    def __init__(self):
        self.calls = []

    def mapped_gameweeks(self):
        self.calls.append(("gameweeks",))
        return [{"id": 1, "name": "Gameweek 1", "is_next": True}]

    def mapped_fixtures(self, gameweek, team_id, future_only, finished):
        self.calls.append(("fixtures", gameweek, team_id, future_only, finished))
        return [{"id": 10, "gameweek": gameweek}]

    def mapped_event_live(self, gameweek):
        self.calls.append(("live", gameweek))
        return {
            "gameweek": gameweek,
            "elements": [
                {"element_id": index, "stats": {"total_points": points}}
                for index, points in enumerate([2, 12, 6, 1], start=1)
            ],
        }

    def mapped_set_piece_notes(self):
        self.calls.append(("set_piece_notes",))
        return {"last_updated": "2026-10-31", "teams": []}

    def mapped_manager_picks(self, entry_id, gameweek):
        self.calls.append(("picks", entry_id, gameweek))
        if entry_id == 404:
            raise OfficialFPLNotFoundError("entry not found")
        return super().mapped_manager_picks(entry_id, gameweek)


class FakeArchive:
    def capture_squad(self, predictions, team):
        pass


class FakeScout:
    POSITIONS = [1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 1, 2, 3, 4]

    def __init__(self):
        self.official_client = FakeOfficialClient()
        self.data_archive = FakeArchive()

    def get_official_predictions(self, gameweek):
        predictions = pd.DataFrame(
            [
                {
                    "id": index,
                    "web_name": f"Player {index}",
                    "team_name": f"Club {index % 6}",
                    "element_type": position,
                    "expected_points": float(index),
                    "availability_factor": 1.0,
                    "selected_by_percent": 20.0,
                    "was_home": True,
                }
                for index, position in enumerate(self.POSITIONS, start=1)
            ]
        )
        predictions.attrs["gameweek"] = gameweek or 1
        predictions.attrs["inference"] = {"strategy": "model-ensemble"}
        predictions.attrs["source"] = "official-fpl"
        return predictions

    def select_optimal_team(self, predictions):
        return predictions.copy()


class MCPEndpointTest(unittest.TestCase):
    def setUp(self):
        self.scout = FakeScout()
        for patcher in (
            patch.object(main, "FPLScout", return_value=self.scout),
            patch.object(src.auth, "VALID_API_KEYS", {API_KEY}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def rpc(self, *messages, token=None):
        """Start the app, post each JSON-RPC message to /mcp, then stop it."""
        headers = dict(MCP_HEADERS)
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        async def run():
            responses = []
            async with main.lifespan(main.app):
                transport = httpx2.ASGITransport(app=main.app)
                async with httpx2.AsyncClient(
                    transport=transport, base_url="https://openfpl.example"
                ) as client:
                    for request_id, (method, params) in enumerate(messages, 1):
                        response = await client.post(
                            "/mcp",
                            headers=headers,
                            json={
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": method,
                                "params": params,
                            },
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                        responses.append(response.json())
            return responses

        return asyncio.run(run())

    def call_tool(self, name, arguments=None, token=None):
        (response,) = self.rpc(
            ("tools/call", {"name": name, "arguments": arguments or {}}),
            token=token,
        )
        return response["result"]

    def structured(self, result):
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def test_initialize_and_list_every_tool_as_read_only(self):
        initialize, listing = self.rpc(
            (
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            ),
            ("tools/list", {}),
        )

        self.assertEqual(initialize["result"]["serverInfo"]["name"], "openfpl")
        tools = {tool["name"]: tool for tool in listing["result"]["tools"]}
        self.assertEqual(set(tools), set(TOOL_ROUTES))
        for name, tool in tools.items():
            self.assertTrue(tool["annotations"]["readOnlyHint"], name)
            self.assertEqual(
                "Requires an OpenFPL API key" in tool["description"],
                name in AUTHENTICATED_TOOLS,
                name,
            )

    def test_tool_tiers_match_their_rest_routes(self):
        authentication = {
            (method, endpoint["path"]): endpoint["authentication"]
            for group in _api_catalog()
            for endpoint in group["endpoints"]
            for method in endpoint["methods"]
        }
        for name, route in TOOL_ROUTES.items():
            expected = "bearer" if name in AUTHENTICATED_TOOLS else "public"
            self.assertEqual(authentication[route], expected, name)

    def test_public_scout_squad_needs_no_key_and_omits_projections(self):
        squad = self.structured(self.call_tool("get_scout_squad", {"gameweek": 3}))

        self.assertEqual(squad["gameweek"], 3)
        self.assertEqual(len(squad["scout_team"]), 15)
        self.assertEqual(squad["player_points"], [])

    def test_public_team_rating_needs_no_key(self):
        rating = self.structured(
            self.call_tool("rate_team", {"entry_id": 7, "gameweek": 1})
        )

        self.assertEqual(rating["team_name"], "Test XI")
        self.assertEqual(len(rating["squad"]), 15)

    def test_public_fixtures_pass_explicit_defaults_to_the_route(self):
        fixtures = self.structured(self.call_tool("get_fixtures", {"gameweek": 4}))
        gameweeks = self.structured(self.call_tool("get_gameweeks"))

        self.assertEqual(fixtures["results"], [{"id": 10, "gameweek": 4}])
        self.assertEqual(gameweeks["count"], 1)
        self.assertIn(("fixtures", 4, None, False, None), self.scout.official_client.calls)

    def test_authenticated_tools_reject_missing_and_invalid_keys(self):
        for token in (None, "wrong-key"):
            for name, arguments in AUTHENTICATED_CALLS.items():
                with self.subTest(tool=name, token=token):
                    result = self.call_tool(name, arguments, token=token)
                    self.assertTrue(result["isError"])
                    self.assertIn(
                        "requires an OpenFPL API key", result["content"][0]["text"]
                    )
        self.assertEqual(self.scout.official_client.calls, [])

    def test_unconfigured_keys_fail_closed(self):
        with patch.object(src.auth, "VALID_API_KEYS", set()):
            result = self.call_tool("get_set_piece_notes", token=API_KEY)

        self.assertTrue(result["isError"])
        self.assertIn("Authentication not properly configured", result["content"][0]["text"])

    def test_player_predictions_filter_sort_and_limit(self):
        result = self.structured(
            self.call_tool(
                "get_player_predictions",
                {"gameweek": 2, "element_type": 3, "limit": 2},
                token=API_KEY,
            )
        )

        # Midfielders are ids 6-9 and 14; expected points equal the id.
        self.assertEqual(result["total"], 5)
        self.assertEqual([player["id"] for player in result["players"]], [14, 9])
        self.assertNotIn("player_points", result)
        self.assertNotIn("scout_team", result)

    def test_live_points_sort_and_limit(self):
        result = self.structured(
            self.call_tool("get_live_points", {"gameweek": 5, "limit": 2}, token=API_KEY)
        )

        self.assertEqual(result["data"]["total"], 4)
        self.assertEqual(
            [player["element_id"] for player in result["data"]["elements"]], [2, 3]
        )

    def test_authenticated_reference_tools_return_official_data(self):
        notes = self.structured(self.call_tool("get_set_piece_notes", token=API_KEY))
        picks = self.structured(
            self.call_tool(
                "get_manager_picks", {"entry_id": 7, "gameweek": 1}, token=API_KEY
            )
        )

        self.assertEqual(notes["official_endpoint"], "/team/set-piece-notes/")
        self.assertEqual(picks["data"]["entry_id"], 7)
        self.assertEqual(len(picks["data"]["picks"]), 15)

    def test_route_errors_and_invalid_arguments_become_tool_errors(self):
        missing = self.call_tool(
            "get_manager_picks", {"entry_id": 404, "gameweek": 1}, token=API_KEY
        )
        out_of_range = self.call_tool("get_scout_squad", {"gameweek": 39})

        self.assertTrue(missing["isError"])
        self.assertIn("404: entry not found", missing["content"][0]["text"])
        self.assertTrue(out_of_range["isError"])

    def test_get_declines_the_sse_stream_instead_of_holding_it_open(self):
        async def run():
            async with main.lifespan(main.app):
                transport = httpx2.ASGITransport(app=main.app)
                async with httpx2.AsyncClient(
                    transport=transport, base_url="https://openfpl.example"
                ) as client:
                    return await asyncio.wait_for(
                        client.get("/mcp", headers={"Accept": "text/event-stream"}),
                        timeout=5,
                    )

        response = asyncio.run(run())

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["allow"], "POST, DELETE")

    def test_endpoint_survives_an_application_restart(self):
        for _ in range(2):
            self.structured(self.call_tool("get_gameweeks"))


class SharedAPIKeyCheckTest(unittest.TestCase):
    def test_bearer_token_parsing(self):
        self.assertEqual(bearer_token("Bearer abc"), "abc")
        self.assertEqual(bearer_token("bearer  abc "), "abc")
        self.assertIsNone(bearer_token("Basic abc"))
        self.assertIsNone(bearer_token("Bearer "))
        self.assertIsNone(bearer_token(None))

    def test_check_api_key(self):
        with patch.object(src.auth, "VALID_API_KEYS", {API_KEY}):
            self.assertEqual(check_api_key(API_KEY), API_KEY)
            for token in (None, "", "other"):
                with self.assertRaises(APIKeyError) as error:
                    check_api_key(token)
                self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()

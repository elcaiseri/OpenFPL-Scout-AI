"""The MCP tools at /mcp, driven over HTTP through the real FastAPI app."""

import asyncio
import unittest
from unittest.mock import patch

import main
import src.auth
from main import _api_catalog
from src.auth import APIKeyError, bearer_token, check_api_key, is_valid_api_key
from tests.mcp_helpers import API_KEY, FakeScout, post_mcp, run_app, sign_in

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
FULL_TIER_CALLS = {
    "get_player_predictions": {"gameweek": 1},
    "get_live_points": {"gameweek": 1},
    "get_set_piece_notes": {},
    "get_manager_picks": {"entry_id": 7, "gameweek": 1},
}


class MCPToolsTest(unittest.TestCase):
    def setUp(self):
        self.scout = FakeScout()
        for patcher in (
            patch.object(main, "FPLScout", return_value=self.scout),
            patch.object(src.auth, "VALID_API_KEYS", {API_KEY}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def rpc(self, *messages, token=API_KEY):
        async def scenario(client):
            responses = []
            for request_id, (method, params) in enumerate(messages, 1):
                response = await post_mcp(client, method, params, token, request_id)
                self.assertEqual(response.status_code, 200, response.text)
                responses.append(response.json())
            return responses

        return run_app(scenario)

    def call_tool(self, name, arguments=None, token=API_KEY):
        (response,) = self.rpc(
            ("tools/call", {"name": name, "arguments": arguments or {}}), token=token
        )
        return response["result"]

    def structured(self, result):
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def free_token(self):
        return run_app(lambda client: sign_in(client, "free"))[1]["access_token"]

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
                name in FULL_TIER_CALLS,
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
            expected = "bearer" if name in FULL_TIER_CALLS else "public"
            self.assertEqual(authentication[route], expected, name)

    def test_requests_without_a_valid_token_get_an_oauth_challenge(self):
        async def scenario(client):
            return [
                await post_mcp(client, "tools/list", token=token)
                for token in (None, "wrong-key")
            ]

        for response in run_app(scenario):
            self.assertEqual(response.status_code, 401)
            self.assertIn(
                'resource_metadata="https://openfpl.kassem.dev/.well-known/'
                'oauth-protected-resource/mcp"',
                response.headers["www-authenticate"],
            )
        self.assertEqual(self.scout.official_client.calls, [])

    def test_free_tier_runs_free_tools(self):
        token = self.free_token()
        squad = self.structured(
            self.call_tool("get_scout_squad", {"gameweek": 3}, token=token)
        )
        rating = self.structured(
            self.call_tool("rate_team", {"entry_id": 7, "gameweek": 1}, token=token)
        )

        self.assertEqual(squad["gameweek"], 3)
        self.assertEqual(len(squad["scout_team"]), 15)
        self.assertEqual(squad["player_points"], [])
        self.assertEqual(rating["team_name"], "Test XI")
        self.assertEqual(len(rating["squad"]), 15)

    def test_free_tier_is_refused_full_tier_tools(self):
        token = self.free_token()
        for name, arguments in FULL_TIER_CALLS.items():
            with self.subTest(tool=name):
                result = self.call_tool(name, arguments, token=token)
                self.assertTrue(result["isError"])
                self.assertIn(
                    "requires an OpenFPL API key", result["content"][0]["text"]
                )
        self.assertEqual(self.scout.official_client.calls, [])

    def test_unconfigured_keys_accept_no_api_key(self):
        async def scenario(client):
            return await post_mcp(client, "tools/list", token=API_KEY)

        with patch.object(src.auth, "VALID_API_KEYS", set()):
            response = run_app(scenario)

        self.assertEqual(response.status_code, 401)

    def test_public_fixtures_pass_explicit_defaults_to_the_route(self):
        fixtures = self.structured(self.call_tool("get_fixtures", {"gameweek": 4}))
        gameweeks = self.structured(self.call_tool("get_gameweeks"))

        self.assertEqual(fixtures["results"], [{"id": 10, "gameweek": 4}])
        self.assertEqual(gameweeks["count"], 1)
        self.assertIn(("fixtures", 4, None, False, None), self.scout.official_client.calls)

    def test_player_predictions_filter_sort_and_limit(self):
        result = self.structured(
            self.call_tool(
                "get_player_predictions", {"gameweek": 2, "element_type": 3, "limit": 2}
            )
        )

        # Midfielders are ids 6-9 and 14; expected points equal the id.
        self.assertEqual(result["total"], 5)
        self.assertEqual([player["id"] for player in result["players"]], [14, 9])
        self.assertNotIn("player_points", result)
        self.assertNotIn("scout_team", result)

    def test_live_points_sort_and_limit(self):
        result = self.structured(
            self.call_tool("get_live_points", {"gameweek": 5, "limit": 2})
        )

        self.assertEqual(result["data"]["total"], 4)
        self.assertEqual(
            [player["element_id"] for player in result["data"]["elements"]], [2, 3]
        )

    def test_full_tier_reference_tools_return_official_data(self):
        notes = self.structured(self.call_tool("get_set_piece_notes"))
        picks = self.structured(
            self.call_tool("get_manager_picks", {"entry_id": 7, "gameweek": 1})
        )

        self.assertEqual(notes["official_endpoint"], "/team/set-piece-notes/")
        self.assertEqual(picks["data"]["entry_id"], 7)
        self.assertEqual(len(picks["data"]["picks"]), 15)

    def test_route_errors_and_invalid_arguments_become_tool_errors(self):
        missing = self.call_tool("get_manager_picks", {"entry_id": 404, "gameweek": 1})
        out_of_range = self.call_tool("get_scout_squad", {"gameweek": 39})

        self.assertTrue(missing["isError"])
        self.assertIn("404: entry not found", missing["content"][0]["text"])
        self.assertTrue(out_of_range["isError"])

    def test_get_declines_the_sse_stream_instead_of_holding_it_open(self):
        async def scenario(client):
            return await asyncio.wait_for(
                client.get(
                    "/mcp",
                    headers={
                        "Accept": "text/event-stream",
                        "Authorization": f"Bearer {API_KEY}",
                    },
                ),
                timeout=5,
            )

        response = run_app(scenario)

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
            self.assertTrue(is_valid_api_key(API_KEY))
            for token in (None, "", "other"):
                self.assertFalse(is_valid_api_key(token))
                with self.assertRaises(APIKeyError) as error:
                    check_api_key(token)
                self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()

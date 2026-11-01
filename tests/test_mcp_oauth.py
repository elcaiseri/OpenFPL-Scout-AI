"""OAuth sign-in for /mcp: discovery, registration, the connect page, tokens."""

import unittest
from unittest.mock import patch

import main
import src.auth
from tests.mcp_helpers import (
    API_KEY,
    REDIRECT_URI,
    FakeScout,
    exchange_code,
    post_mcp,
    query,
    register,
    run_app,
    sign_in,
    start_authorization,
    submit_connect,
)


class MCPOAuthTest(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.object(main, "FPLScout", return_value=FakeScout()),
            patch.object(src.auth, "VALID_API_KEYS", {API_KEY}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_discovery_metadata_points_clients_at_this_site(self):
        async def scenario(client):
            resource = await client.get("/.well-known/oauth-protected-resource/mcp")
            server = await client.get("/.well-known/oauth-authorization-server")
            return resource.json(), server.json()

        resource, server = run_app(scenario)

        self.assertEqual(resource["resource"], "https://openfpl.kassem.dev/mcp")
        self.assertEqual(
            resource["authorization_servers"], ["https://openfpl.kassem.dev"]
        )
        self.assertEqual(server["issuer"], "https://openfpl.kassem.dev")
        self.assertEqual(
            server["registration_endpoint"], "https://openfpl.kassem.dev/register"
        )
        self.assertIn("S256", server["code_challenge_methods_supported"])

    def test_registered_clients_need_no_server_storage(self):
        async def scenario(client):
            registered = await register(client)
            stored = await main.mcp_oauth.get_client(registered["client_id"])
            return registered, stored

        registered, stored = run_app(scenario)

        self.assertEqual(stored.client_name, "Claude")
        self.assertEqual(stored.client_secret, registered["client_secret"])
        self.assertEqual([str(uri) for uri in stored.redirect_uris], [REDIRECT_URI])

    def test_connect_page_names_the_client_and_where_it_returns(self):
        async def scenario(client):
            registered = await register(client, name="Claude <b>")
            connect_url, _ = await start_authorization(client, registered)
            return await client.get(connect_url)

        page = run_app(scenario)

        self.assertEqual(page.status_code, 200)
        self.assertIn("Claude &lt;b&gt;", page.text)
        self.assertIn("claude.ai", page.text)
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(page.headers["cache-control"], "no-store")

    def test_api_key_sign_in_unlocks_the_full_tier(self):
        async def scenario(client):
            _, tokens = await sign_in(client, "full", API_KEY)
            response = await post_mcp(
                client,
                "tools/call",
                {"name": "get_set_piece_notes", "arguments": {}},
                tokens["access_token"],
            )
            return tokens, response.json()["result"]

        tokens, result = run_app(scenario)

        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["expires_in"], 3600)
        self.assertNotIn(API_KEY, tokens["access_token"])
        self.assertNotIn(API_KEY, tokens["refresh_token"])
        self.assertFalse(result.get("isError"), result)

    def test_public_clients_without_a_secret_can_sign_in(self):
        registered, tokens = run_app(
            lambda client: sign_in(client, "free", auth_method="none")
        )

        self.assertNotIn("client_secret", registered)
        self.assertIn("access_token", tokens)

    def test_invalid_api_key_stays_on_the_page(self):
        async def scenario(client):
            registered = await register(client)
            connect_url, _ = await start_authorization(client, registered)
            return await submit_connect(client, connect_url, "full", "not-a-key")

        response = run_app(scenario)

        self.assertEqual(response.status_code, 401)
        self.assertIn("That API key is not valid.", response.text)

    def test_cancel_returns_access_denied_to_the_client(self):
        async def scenario(client):
            registered = await register(client)
            connect_url, _ = await start_authorization(client, registered)
            return await submit_connect(client, connect_url, "cancel")

        response = run_app(scenario)
        location = response.headers["location"]

        self.assertTrue(location.startswith(REDIRECT_URI))
        self.assertEqual(query(location)["error"], "access_denied")
        self.assertEqual(query(location)["state"], "state-123")

    def test_tampered_sign_in_request_is_rejected(self):
        async def scenario(client):
            return await client.post(
                "/oauth/connect", data={"request": "forged", "action": "free"}
            )

        response = run_app(scenario)

        self.assertEqual(response.status_code, 400)
        self.assertIn("expired or is invalid", response.text)

    def test_code_needs_the_matching_client_and_verifier(self):
        async def scenario(client):
            registered = await register(client)
            other = await register(client)
            connect_url, verifier = await start_authorization(client, registered)
            done = await submit_connect(client, connect_url, "free")
            code = query(done.headers["location"])["code"]
            wrong_verifier = await exchange_code(client, registered, code, "x" * 64)
            wrong_client = await exchange_code(client, other, code, verifier)
            return wrong_verifier, wrong_client

        wrong_verifier, wrong_client = run_app(scenario)

        self.assertEqual(wrong_verifier.status_code, 400)
        self.assertEqual(wrong_verifier.json()["error"], "invalid_grant")
        self.assertEqual(wrong_client.status_code, 400)
        self.assertEqual(wrong_client.json()["error"], "invalid_grant")

    def test_tokens_for_another_resource_are_refused(self):
        async def scenario(client):
            registered = await register(client)
            response = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": registered["client_id"],
                    "redirect_uri": REDIRECT_URI,
                    "code_challenge": "c" * 43,
                    "code_challenge_method": "S256",
                    "state": "s",
                    "resource": "https://elsewhere.example/mcp",
                },
            )
            return response

        location = run_app(scenario).headers["location"]

        self.assertEqual(query(location)["error"], "invalid_target")

    def test_refresh_keeps_the_tier(self):
        async def scenario(client):
            registered, tokens = await sign_in(client, "full", API_KEY)
            refreshed = await client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": registered["client_id"],
                    "client_secret": registered["client_secret"],
                },
            )
            access = refreshed.json()["access_token"]
            return refreshed.status_code, await main.mcp_oauth.tier_for(f"Bearer {access}")

        status, tier = run_app(scenario)

        self.assertEqual(status, 200)
        self.assertEqual(tier, "full")

    def test_removing_the_api_key_revokes_its_sign_ins(self):
        async def scenario(client):
            registered, tokens = await sign_in(client, "full", API_KEY)
            with patch.object(src.auth, "VALID_API_KEYS", {"replacement-key"}):
                call = await post_mcp(client, "tools/list", token=tokens["access_token"])
                refreshed = await client.post(
                    "/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": tokens["refresh_token"],
                        "client_id": registered["client_id"],
                        "client_secret": registered["client_secret"],
                    },
                )
            return call, refreshed

        call, refreshed = run_app(scenario)

        self.assertEqual(call.status_code, 401)
        self.assertEqual(refreshed.status_code, 400)
        self.assertEqual(refreshed.json()["error"], "invalid_grant")

    def test_free_sign_ins_survive_key_changes(self):
        async def scenario(client):
            _, tokens = await sign_in(client, "free")
            with patch.object(src.auth, "VALID_API_KEYS", set()):
                return await post_mcp(client, "tools/list", token=tokens["access_token"])

        self.assertEqual(run_app(scenario).status_code, 200)


if __name__ == "__main__":
    unittest.main()

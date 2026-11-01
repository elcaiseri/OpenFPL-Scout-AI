"""Shared helpers for driving /mcp and its OAuth sign-in through the real app."""

import asyncio
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx2
import pandas as pd

import main
from src.official_fpl import OfficialFPLNotFoundError
from tests.test_api_schema import FakeRatingOfficialClient

API_KEY = "test-key"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
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


def run_app(scenario):
    """Start the app, run ``await scenario(client)``, then stop the app."""

    async def run():
        async with main.lifespan(main.app):
            transport = httpx2.ASGITransport(app=main.app)
            async with httpx2.AsyncClient(
                transport=transport, base_url=main.SITE_URL
            ) as client:
                return await scenario(client)

    return asyncio.run(run())


async def post_mcp(client, method, params=None, token=None, request_id=1):
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )


def pkce_pair():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def query(url):
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


async def register(client, auth_method="client_secret_post", name="Claude"):
    response = await client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": auth_method,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def start_authorization(client, registered, resource=None):
    """Follow /authorize to the connect page; return (connect URL, verifier)."""
    verifier, challenge = pkce_pair()
    response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "state-123",
            "resource": resource or main.mcp_oauth.resource_url,
        },
    )
    assert response.status_code in (302, 307), response.text
    return response.headers["location"], verifier


async def submit_connect(client, connect_url, action, api_key=None):
    form = {"request": query(connect_url)["request"], "action": action}
    if api_key is not None:
        form["api_key"] = api_key
    return await client.post("/oauth/connect", data=form)


async def exchange_code(client, registered, code, verifier):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": registered["client_id"],
        "code_verifier": verifier,
        "resource": main.mcp_oauth.resource_url,
    }
    if registered.get("client_secret"):
        form["client_secret"] = registered["client_secret"]
    return await client.post("/token", data=form)


async def sign_in(client, action="free", api_key=None, auth_method="client_secret_post"):
    """Run the whole flow and return (registered client, token response JSON)."""
    registered = await register(client, auth_method)
    connect_url, verifier = await start_authorization(client, registered)
    done = await submit_connect(client, connect_url, action, api_key)
    assert done.status_code == 302, done.text
    tokens = await exchange_code(client, registered, query(done.headers["location"])["code"], verifier)
    assert tokens.status_code == 200, tokens.text
    return registered, tokens.json()

"""OpenFPL as a remote MCP server, served at ``/mcp`` inside the FastAPI app.

Every tool delegates to a coroutine that the REST API already runs, so a tool
and its REST endpoint cannot drift apart. Public tools mirror public REST
routes; the rest need the same ``VALID_API_KEYS`` bearer token that the REST
API checks, sent by the MCP client as ``Authorization: Bearer <key>``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable, Optional

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from src.auth import APIKeyError, bearer_token, check_api_key
from src.models import PlayerPointsModel

Gameweek = Annotated[int, Field(ge=1, le=38, description="Gameweek number (1-38)")]
EntryId = Annotated[int, Field(ge=1, description="Public FPL manager team ID")]
Limit = Annotated[
    int, Field(ge=1, le=1000, description="Maximum number of players to return")
]

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=True
)

INSTRUCTIONS = (
    "OpenFPL Scout AI: model-ensemble Fantasy Premier League projections, "
    "optimized 15-player squads, manager team ratings, and official FPL data. "
    "Call get_gameweeks first when the user does not name a gameweek. Tools "
    "marked 'Requires an OpenFPL API key' need the connection to send "
    "'Authorization: Bearer <key>'."
)


@dataclass(frozen=True)
class OpenFPLHandlers:
    """The REST API coroutines each MCP tool delegates to."""

    scout_squad: Callable[[Optional[int]], Awaitable[BaseModel]]
    rate_team: Callable[[int, int], Awaitable[BaseModel]]
    gameweeks: Callable[[], Awaitable[BaseModel]]
    fixtures: Callable[[Optional[int]], Awaitable[BaseModel]]
    player_predictions: Callable[[PlayerPointsModel, str], Awaitable[BaseModel]]
    live_points: Callable[[int], Awaitable[BaseModel]]
    set_piece_notes: Callable[[], Awaitable[BaseModel]]
    manager_picks: Callable[[int, int], Awaitable[BaseModel]]


async def _run(call: Awaitable[BaseModel]) -> dict[str, Any]:
    """Await a REST handler and turn its HTTP errors into MCP tool errors."""
    try:
        result = await call
    except HTTPException as error:
        raise ToolError(f"{error.status_code}: {error.detail}") from error
    return result.model_dump(mode="json")


def _require_api_key(ctx: Context) -> str:
    """Return the caller's API key, or fail the tool call like the REST API."""
    token = bearer_token((ctx.headers or {}).get("authorization"))
    try:
        return check_api_key(token)
    except APIKeyError as error:
        if error.status_code == 401:
            raise ToolError(
                "This tool requires an OpenFPL API key. Configure the MCP "
                "connection to send 'Authorization: Bearer <key>'."
            ) from error
        raise ToolError(f"{error.status_code}: {error.detail}") from error


def _top(players: list[dict[str, Any]], key: Callable[[dict], Any], limit: int):
    """Rank players by ``key`` (highest first) and keep the first ``limit``."""

    def score(player: dict[str, Any]) -> float:
        value = key(player)
        return float(value) if isinstance(value, (int, float)) else float("-inf")

    return sorted(players, key=score, reverse=True)[:limit]


def create_mcp_server(handlers: OpenFPLHandlers, version: str = "") -> MCPServer:
    """Build the OpenFPL MCP server with its public and authenticated tools."""
    server = MCPServer(
        name="openfpl",
        title="OpenFPL Scout AI",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/elcaiseri/OpenFPL-Scout-AI",
        version=version,
    )

    # Public tools: the same data the web app serves without a key.

    @server.tool(title="Get AI scout squad", annotations=READ_ONLY)
    async def get_scout_squad(gameweek: Optional[Gameweek] = None) -> dict[str, Any]:
        """Generate OpenFPL's optimized 15-player squad for a gameweek.

        Players come from the model ensemble's expected points under FPL squad
        rules. Omit gameweek for the next playable gameweek. After the
        deadline the response is the frozen pre-deadline forecast
        (``frozen: true``).
        """
        return await _run(handlers.scout_squad(gameweek))

    @server.tool(title="Rate an FPL team", annotations=READ_ONLY)
    async def rate_team(entry_id: EntryId, gameweek: Gameweek) -> dict[str, Any]:
        """Rate a manager's published FPL squad against the AI benchmark.

        Returns a 0-100 rating and grade, projected points versus the AI
        squad, captaincy, strengths, risks, and the scored squad. The current
        gameweek's picks become public only after its deadline.
        """
        return await _run(handlers.rate_team(entry_id, gameweek))

    @server.tool(title="List gameweeks", annotations=READ_ONLY)
    async def get_gameweeks() -> dict[str, Any]:
        """List all 38 official FPL gameweeks.

        Each has its deadline, finished/current/next flags, average and
        highest scores, and chip plays.
        """
        return await _run(handlers.gameweeks())

    @server.tool(title="Get fixtures", annotations=READ_ONLY)
    async def get_fixtures(gameweek: Optional[Gameweek] = None) -> dict[str, Any]:
        """List official Premier League fixtures with named teams and FDR.

        Omit gameweek for the whole season.
        """
        return await _run(handlers.fixtures(gameweek))

    # Authenticated tools: the same split as the REST API's bearer routes.

    @server.tool(title="Get player projections", annotations=READ_ONLY)
    async def get_player_predictions(
        ctx: Context,
        gameweek: Gameweek,
        element_type: Annotated[
            Optional[int],
            Field(ge=1, le=4, description="Position: GK=1, DEF=2, MID=3, FWD=4"),
        ] = None,
        web_name: Annotated[
            Optional[str], Field(description="Exact player web name, any case")
        ] = None,
        team_name: Annotated[
            Optional[str], Field(description="Exact club name, any case")
        ] = None,
        was_home: Annotated[
            Optional[bool],
            Field(description="Only players whose every fixture is at home/away"),
        ] = None,
        limit: Limit = 50,
    ) -> dict[str, Any]:
        """Get model-ensemble expected points for players in a gameweek.

        Requires an OpenFPL API key. Results are sorted by expected points
        and cut to ``limit``; ``total`` is the number of matching players.
        """
        api_key = _require_api_key(ctx)
        filters = {
            name: value
            for name, value in {
                "element_type": element_type,
                "web_name": web_name,
                "team_name": team_name,
                "was_home": was_home,
            }.items()
            if value is not None
        }
        response = await _run(
            handlers.player_predictions(
                PlayerPointsModel(gameweek=gameweek, **filters), api_key
            )
        )
        players = response.pop("player_points")
        response.pop("scout_team", None)
        response["total"] = len(players)
        response["players"] = _top(
            players, lambda player: player.get("expected_points"), limit
        )
        return response

    @server.tool(title="Get live gameweek points", annotations=READ_ONLY)
    async def get_live_points(
        ctx: Context, gameweek: Gameweek, limit: Limit = 50
    ) -> dict[str, Any]:
        """Get official live FPL scoring for a gameweek.

        Requires an OpenFPL API key. Players are sorted by total points and
        cut to ``limit``; ``total`` is the number of players with live data.
        """
        _require_api_key(ctx)
        response = await _run(handlers.live_points(gameweek))
        players = response["data"].get("elements", [])
        response["data"]["total"] = len(players)
        response["data"]["elements"] = _top(
            players, lambda player: (player.get("stats") or {}).get("total_points"), limit
        )
        return response

    @server.tool(title="Get set-piece notes", annotations=READ_ONLY)
    async def get_set_piece_notes(ctx: Context) -> dict[str, Any]:
        """Get the official FPL set-piece taker notes for every club.

        Requires an OpenFPL API key.
        """
        _require_api_key(ctx)
        return await _run(handlers.set_piece_notes())

    @server.tool(title="Get manager picks", annotations=READ_ONLY)
    async def get_manager_picks(
        ctx: Context, entry_id: EntryId, gameweek: Gameweek
    ) -> dict[str, Any]:
        """Get a manager's official picks, captaincy, chip, and bank for a gameweek.

        Requires an OpenFPL API key. Picks become public after the deadline.
        """
        _require_api_key(ctx)
        return await _run(handlers.manager_picks(entry_id, gameweek))

    return server


class MCPEndpoint:
    """ASGI endpoint for the MCP Streamable HTTP transport.

    The SDK's session manager runs once per instance, so ``lifespan`` builds a
    fresh one each time the app starts. The transport is stateless with JSON
    responses: any Cloud Run instance can answer any request, and no session
    affinity is needed.
    """

    def __init__(self, server: MCPServer, path: str = "/mcp"):
        self.server = server
        self.path = path
        self._running = False

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        self.server.streamable_http_app(
            streamable_http_path=self.path,
            stateless_http=True,
            json_response=True,
            # The SDK enables DNS-rebinding protection for a localhost host,
            # which rejects every public Host header. It guards servers on
            # a user's own machine; this one is public and authenticates
            # with bearer headers, never cookies.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
        async with self.server.session_manager.run():
            self._running = True
            try:
                yield
            finally:
                self._running = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._running:
            raise RuntimeError("MCP endpoint used outside the application lifespan")
        if scope["method"] == "GET":
            # A GET opens a standalone SSE stream for server-initiated
            # messages. A stateless server never sends any, so the stream
            # would only hold a Cloud Run connection open until it times
            # out. The transport spec allows 405 for "no SSE stream here".
            response = PlainTextResponse(
                "This MCP endpoint does not offer an SSE stream; POST JSON-RPC.",
                status_code=405,
                headers={"Allow": "POST, DELETE"},
            )
            await response(scope, receive, send)
            return
        await self.server.session_manager.asgi_app(scope, receive, send)

"""OpenFPL Scout API backed exclusively by official FPL runtime data."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Literal, Mapping, Optional

import aiofiles
from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from src.auth import verify_api_key
from src.logger import get_logger
from src.models import (
    APICatalogModel,
    OfficialFPLCollectionModel,
    OfficialFPLItemModel,
    PlayerPointsModel,
    ResponseModel,
)
from src.official_fpl import OfficialFPLAPIError, OfficialFPLNotFoundError
from src.scout import FPLScout, InferenceError
from src.utils import load_config

logger = get_logger(__name__)

config = load_config("config/config.yaml")
scout: FPLScout


def _is_production_environment(
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """Return whether public developer tooling should be restricted."""
    values = os.environ if environment is None else environment
    configured_environment = values.get("OPENFPL_ENV", "").strip().lower()
    return configured_environment in {"prod", "production"} or bool(
        values.get("K_SERVICE")
    )


def _documentation_config(is_production: bool) -> dict[str, Optional[str]]:
    """Configure one production docs UI while retaining local Swagger tooling."""
    return {
        "docs_url": None if is_production else "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
        "swagger_ui_oauth2_redirect_url": (
            None if is_production else "/docs/oauth2-redirect"
        ),
    }


def _documentation_links(is_production: bool) -> dict[str, str]:
    """Return the documentation links advertised by the API catalog."""
    if is_production:
        return {"redoc": "/redoc"}
    return {
        "swagger": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }


IS_PRODUCTION = _is_production_environment()

OPENAPI_TAGS = [
    {
        "name": "Service",
        "description": "API discovery, documentation links, and service health.",
    },
    {
        "name": "Scout AI",
        "description": "Model-ensemble player projections and optimized FPL squads.",
    },
    {
        "name": "Official FPL · Gameweeks",
        "description": "Mapped event deadlines, scores, and current-season state.",
    },
    {
        "name": "Official FPL · Teams",
        "description": "Mapped official clubs and strength ratings.",
    },
    {
        "name": "Official FPL · Players",
        "description": "Mapped player identities, availability, prices, and history.",
    },
    {
        "name": "Official FPL · Fixtures",
        "description": "Mapped fixtures, opponents, scores, and difficulty ratings.",
    },
    {
        "name": "Official FPL · Managers",
        "description": "Public manager profiles, season history, picks, and transfers.",
    },
    {
        "name": "Official FPL · Leagues & Cups",
        "description": "Classic and head-to-head standings, matches, and cup state.",
    },
    {
        "name": "Official FPL · Reference & Rankings",
        "description": "Regions, set-piece notes, winners, and official rankings.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scout
    logger.info("Initializing application with official FPL data")
    scout = FPLScout(config)
    logger.info("FPLScout initialized and ready")
    yield
    logger.info("Shutting down application")


app = FastAPI(
    title="OpenFPL API",
    description="AI-powered FPL Scout using official Fantasy Premier League data",
    version=config.get("version", "1.0.0"),
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
    **_documentation_config(IS_PRODUCTION),
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")


def _uses_bearer_auth(dependency) -> bool:
    if dependency.call is verify_api_key:
        return True
    return any(_uses_bearer_auth(item) for item in dependency.dependencies)


def _api_catalog():
    """Build the catalog from registered routes so it cannot drift from OpenAPI."""
    descriptions = {item["name"]: item["description"] for item in OPENAPI_TAGS}
    groups = []
    for tag in (item["name"] for item in OPENAPI_TAGS):
        endpoints = []
        for route in app.routes:
            if not isinstance(route, APIRoute) or tag not in route.tags:
                continue
            methods = sorted(method for method in route.methods if method != "HEAD")
            endpoints.append(
                {
                    "methods": methods,
                    "path": route.path,
                    "summary": route.summary or route.name.replace("_", " ").title(),
                    "authentication": (
                        "bearer" if _uses_bearer_auth(route.dependant) else "public"
                    ),
                    "source": "official-fpl"
                    if tag.startswith("Official FPL") or tag == "Scout AI"
                    else "openfpl",
                }
            )
        if endpoints:
            groups.append(
                {
                    "name": tag,
                    "description": descriptions[tag],
                    "endpoints": sorted(
                        endpoints, key=lambda item: (item["path"], item["methods"])
                    ),
                }
            )
    return groups


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_index():
    """Serve the OpenFPL web application."""
    try:
        async with aiofiles.open("static/index.html", "r") as file:
            return HTMLResponse(content=await file.read())
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=404, detail="static/index.html not found"
        ) from error
    except OSError as error:
        logger.exception("Failed to read index.html")
        raise HTTPException(
            status_code=500, detail="Failed to read index.html"
        ) from error


@app.get(
    "/api",
    response_model=APICatalogModel,
    tags=["Service"],
    summary="List the complete OpenFPL API",
    operation_id="get_api_catalog",
)
async def get_api_info():
    """Return every public and authenticated route grouped by OpenAPI tag."""
    return {
        "message": "OpenFPL — Official FPL data, AI squad projections",
        "version": config.get("version", "1.0.0"),
        "source": "https://fantasy.premierleague.com/api/",
        "documentation": _documentation_links(IS_PRODUCTION),
        "tags": _api_catalog(),
    }


@app.get(
    "/api/health",
    tags=["Service"],
    summary="Check service health",
    operation_id="get_service_health",
)
async def check_health():
    """Return service and configured data-source status."""
    return {
        "status": "healthy",
        "source": "official-fpl",
        "models": len(scout.model_artifacts),
    }


@app.get(
    "/api/gameweeks",
    tags=["Official FPL · Gameweeks"],
    summary="List playable gameweeks",
    operation_id="get_available_gameweeks",
)
async def get_available_gameweeks():
    """Return gameweeks exposed by the official FPL event state."""
    try:
        return await run_in_threadpool(scout.official_client.available_gameweeks)
    except OfficialFPLAPIError as error:
        logger.exception("Failed to load official gameweeks")
        raise HTTPException(status_code=502, detail=str(error)) from error


async def _official_call(function: Callable[..., Any], *args, **kwargs) -> Any:
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except OfficialFPLNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except OfficialFPLAPIError as error:
        logger.exception("Official FPL data request failed")
        raise HTTPException(status_code=502, detail=str(error)) from error


def _official_collection(
    endpoint: str, results, total: Optional[int] = None
) -> OfficialFPLCollectionModel:
    return OfficialFPLCollectionModel(
        official_endpoint=endpoint,
        count=len(results),
        total=len(results) if total is None else total,
        results=results,
    )


def _official_item(endpoint: str, data: dict) -> OfficialFPLItemModel:
    return OfficialFPLItemModel(official_endpoint=endpoint, data=data)


@app.get(
    "/api/fpl/gameweeks",
    response_model=OfficialFPLCollectionModel,
    tags=["Official FPL · Gameweeks"],
    summary="Map all official FPL gameweeks",
    operation_id="get_official_fpl_gameweeks",
)
async def get_official_fpl_gameweeks():
    results = await _official_call(scout.official_client.mapped_gameweeks)
    return _official_collection("/bootstrap-static/", results)


@app.get(
    "/api/fpl/teams",
    response_model=OfficialFPLCollectionModel,
    tags=["Official FPL · Teams"],
    summary="Map all official FPL teams",
    operation_id="get_official_fpl_teams",
)
async def get_official_fpl_teams():
    results = await _official_call(scout.official_client.mapped_teams)
    return _official_collection("/bootstrap-static/", results)


@app.get(
    "/api/fpl/players",
    response_model=OfficialFPLCollectionModel,
    tags=["Official FPL · Players"],
    summary="Map and filter official FPL players",
    operation_id="get_official_fpl_players",
)
async def get_official_fpl_players(
    team_id: Optional[int] = Query(None, ge=1, description="Official FPL team ID"),
    element_type: Optional[int] = Query(
        None, ge=1, le=4, description="Position: GK=1, DEF=2, MID=3, FWD=4"
    ),
    selectable_only: bool = Query(
        False, description="Return only players currently selectable in FPL"
    ),
    status: Optional[str] = Query(
        None, min_length=1, max_length=1, description="Official availability code"
    ),
    search: Optional[str] = Query(
        None, min_length=1, description="Case-insensitive player name search"
    ),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    order_by: Literal[
        "id", "web_name", "price", "total_points", "selected_by_percent"
    ] = Query("id"),
    descending: bool = Query(False),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
):
    results = await _official_call(
        scout.official_client.mapped_players,
        team_id,
        element_type,
        selectable_only,
    )
    if status is not None:
        results = [
            player
            for player in results
            if str(player.get("status") or "").casefold() == status.casefold()
        ]
    if search is not None:
        needle = search.casefold()
        results = [
            player
            for player in results
            if needle
            in " ".join(
                str(player.get(field) or "")
                for field in ("web_name", "first_name", "second_name")
            ).casefold()
        ]
    if min_price is not None:
        results = [
            player
            for player in results
            if player.get("price") is not None and player["price"] >= min_price
        ]
    if max_price is not None:
        results = [
            player
            for player in results
            if player.get("price") is not None and player["price"] <= max_price
        ]

    populated = [player for player in results if player.get(order_by) is not None]
    missing = [player for player in results if player.get(order_by) is None]
    populated.sort(
        key=lambda player: str(player[order_by]).casefold()
        if order_by == "web_name"
        else player[order_by],
        reverse=descending,
    )
    results = populated + missing
    total = len(results)
    return _official_collection(
        "/bootstrap-static/", results[offset : offset + limit], total=total
    )


@app.get(
    "/api/fpl/players/{player_id}",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Players"],
    summary="Map one official FPL player",
    operation_id="get_official_fpl_player",
)
async def get_official_fpl_player(
    player_id: int,
):
    result = await _official_call(scout.official_client.mapped_player, player_id)
    return _official_item("/bootstrap-static/", result)


@app.get(
    "/api/fpl/players/{player_id}/history",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Players"],
    summary="Map a player's official history and fixtures",
    operation_id="get_official_fpl_player_history",
)
async def get_official_fpl_player_history(
    player_id: int,
):
    result = await _official_call(
        scout.official_client.mapped_player_summary, player_id
    )
    return _official_item(f"/element-summary/{player_id}/", result)


@app.get(
    "/api/fpl/fixtures",
    response_model=OfficialFPLCollectionModel,
    tags=["Official FPL · Fixtures"],
    summary="Map and filter official FPL fixtures",
    operation_id="get_official_fpl_fixtures",
)
async def get_official_fpl_fixtures(
    gameweek: Optional[int] = Query(None, ge=1, le=38),
    team_id: Optional[int] = Query(None, ge=1, description="Official FPL team ID"),
    future_only: bool = Query(False, description="Return only unstarted fixtures"),
    finished: Optional[bool] = Query(
        None, description="Filter by official finished state"
    ),
):
    results = await _official_call(
        scout.official_client.mapped_fixtures,
        gameweek,
        team_id,
        future_only,
        finished,
    )
    return _official_collection("/fixtures/", results)


@app.get(
    "/api/fpl/gameweeks/status",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Gameweeks"],
    summary="Get official event processing status",
    operation_id="get_official_fpl_event_status",
)
async def get_official_fpl_event_status():
    result = await _official_call(scout.official_client.event_status)
    return _official_item("/event-status/", dict(result))


@app.get(
    "/api/fpl/gameweeks/{gameweek}/live",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Gameweeks"],
    summary="Get mapped live gameweek scoring",
    operation_id="get_official_fpl_live_gameweek",
)
async def get_official_fpl_live_gameweek(
    gameweek: int = Path(..., ge=1, le=38),
):
    result = await _official_call(
        scout.official_client.mapped_event_live, gameweek
    )
    return _official_item(f"/event/{gameweek}/live/", result)


@app.get(
    "/api/fpl/dream-team",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Gameweeks"],
    summary="Get the official season dream team",
    operation_id="get_official_fpl_season_dream_team",
)
async def get_official_fpl_season_dream_team():
    result = await _official_call(scout.official_client.mapped_dream_team)
    return _official_item("/dream-team/", result)


@app.get(
    "/api/fpl/gameweeks/{gameweek}/dream-team",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Gameweeks"],
    summary="Get an official gameweek dream team",
    operation_id="get_official_fpl_gameweek_dream_team",
)
async def get_official_fpl_gameweek_dream_team(
    gameweek: int = Path(..., ge=1, le=38),
):
    result = await _official_call(
        scout.official_client.mapped_dream_team, gameweek
    )
    return _official_item(f"/dream-team/{gameweek}/", result)


@app.get(
    "/api/fpl/fixtures/{fixture_id}/stats",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Fixtures"],
    summary="Get mapped official fixture statistics",
    operation_id="get_official_fpl_fixture_stats",
)
async def get_official_fpl_fixture_stats(
    fixture_id: int = Path(..., ge=1),
):
    result = await _official_call(
        scout.official_client.mapped_fixture_stats, fixture_id
    )
    return _official_item(f"/fixture/{fixture_id}/stats/", result)


@app.get(
    "/api/fpl/managers/{entry_id}",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Managers"],
    summary="Get a mapped public manager profile",
    operation_id="get_official_fpl_manager",
)
async def get_official_fpl_manager(entry_id: int = Path(..., ge=1)):
    result = await _official_call(scout.official_client.mapped_manager, entry_id)
    return _official_item(f"/entry/{entry_id}/", result)


@app.get(
    "/api/fpl/managers/{entry_id}/history",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Managers"],
    summary="Get a manager's official season history",
    operation_id="get_official_fpl_manager_history",
)
async def get_official_fpl_manager_history(entry_id: int = Path(..., ge=1)):
    result = await _official_call(scout.official_client.manager_history, entry_id)
    return _official_item(f"/entry/{entry_id}/history/", result)


@app.get(
    "/api/fpl/managers/{entry_id}/transfers",
    response_model=OfficialFPLCollectionModel,
    tags=["Official FPL · Managers"],
    summary="Get a manager's public official transfers",
    operation_id="get_official_fpl_manager_transfers",
)
async def get_official_fpl_manager_transfers(
    entry_id: int = Path(..., ge=1),
    gameweek: Optional[int] = Query(None, ge=1, le=38),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
):
    results = await _official_call(
        scout.official_client.mapped_manager_transfers, entry_id
    )
    if gameweek is not None:
        results = [item for item in results if item.get("event") == gameweek]
    total = len(results)
    return _official_collection(
        f"/entry/{entry_id}/transfers/",
        results[offset : offset + limit],
        total=total,
    )


@app.get(
    "/api/fpl/managers/{entry_id}/gameweeks/{gameweek}/picks",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Managers"],
    summary="Get a manager's official gameweek picks",
    operation_id="get_official_fpl_manager_picks",
)
async def get_official_fpl_manager_picks(
    entry_id: int = Path(..., ge=1),
    gameweek: int = Path(..., ge=1, le=38),
):
    result = await _official_call(
        scout.official_client.mapped_manager_picks, entry_id, gameweek
    )
    return _official_item(
        f"/entry/{entry_id}/event/{gameweek}/picks/", result
    )


@app.get(
    "/api/fpl/leagues/classic/{league_id}/standings",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Leagues & Cups"],
    summary="Get paginated classic-league standings",
    operation_id="get_official_fpl_classic_league_standings",
)
async def get_official_fpl_classic_league_standings(
    league_id: int = Path(..., ge=1),
    page_standings: int = Query(1, ge=1),
    page_new_entries: int = Query(1, ge=1),
    phase: int = Query(1, ge=1),
):
    result = await _official_call(
        scout.official_client.classic_league_standings,
        league_id,
        page_standings,
        page_new_entries,
        phase,
    )
    endpoint = (
        f"/leagues-classic/{league_id}/standings/"
        f"?page_new_entries={page_new_entries}"
        f"&page_standings={page_standings}&phase={phase}"
    )
    return _official_item(endpoint, result)


@app.get(
    "/api/fpl/leagues/h2h/{league_id}/standings",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Leagues & Cups"],
    summary="Get paginated head-to-head standings",
    operation_id="get_official_fpl_h2h_league_standings",
)
async def get_official_fpl_h2h_league_standings(
    league_id: int = Path(..., ge=1),
    page_standings: int = Query(1, ge=1),
    page_new_entries: int = Query(1, ge=1),
):
    result = await _official_call(
        scout.official_client.h2h_league_standings,
        league_id,
        page_standings,
        page_new_entries,
    )
    endpoint = (
        f"/leagues-h2h/{league_id}/standings/"
        f"?page_new_entries={page_new_entries}"
        f"&page_standings={page_standings}"
    )
    return _official_item(endpoint, result)


@app.get(
    "/api/fpl/leagues/h2h/{league_id}/matches",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Leagues & Cups"],
    summary="Get paginated head-to-head matches",
    operation_id="get_official_fpl_h2h_league_matches",
)
async def get_official_fpl_h2h_league_matches(
    league_id: int = Path(..., ge=1),
    page: int = Query(1, ge=1),
    entry_id: Optional[int] = Query(None, ge=1),
    gameweek: Optional[int] = Query(None, ge=1, le=38),
):
    result = await _official_call(
        scout.official_client.h2h_league_matches,
        league_id,
        page,
        entry_id,
        gameweek,
    )
    return _official_item(
        f"/leagues-h2h-matches/league/{league_id}/", result
    )


@app.get(
    "/api/fpl/leagues/{league_id}/cup-status",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Leagues & Cups"],
    summary="Get official league cup status",
    operation_id="get_official_fpl_league_cup_status",
)
async def get_official_fpl_league_cup_status(
    league_id: int = Path(..., ge=1),
):
    result = await _official_call(
        scout.official_client.league_cup_status, league_id
    )
    return _official_item(f"/league/{league_id}/cup-status/", result)


@app.get(
    "/api/fpl/regions",
    response_model=OfficialFPLCollectionModel,
    tags=["Official FPL · Reference & Rankings"],
    summary="List official FPL manager regions",
    operation_id="get_official_fpl_regions",
)
async def get_official_fpl_regions():
    results = await _official_call(scout.official_client.regions)
    return _official_collection("/regions/", results)


@app.get(
    "/api/fpl/set-piece-notes",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Reference & Rankings"],
    summary="Get mapped official set-piece notes",
    operation_id="get_official_fpl_set_piece_notes",
)
async def get_official_fpl_set_piece_notes():
    result = await _official_call(scout.official_client.mapped_set_piece_notes)
    return _official_item("/team/set-piece-notes/", result)


@app.get(
    "/api/fpl/rankings/best-private-leagues",
    response_model=OfficialFPLCollectionModel,
    tags=["Official FPL · Reference & Rankings"],
    summary="Get official best private leagues",
    operation_id="get_official_fpl_best_private_leagues",
)
async def get_official_fpl_best_private_leagues():
    results = await _official_call(scout.official_client.best_private_leagues)
    return _official_collection("/stats/best-classic-private-leagues/", results)


@app.get(
    "/api/fpl/rankings/most-valuable-teams",
    response_model=OfficialFPLCollectionModel,
    tags=["Official FPL · Reference & Rankings"],
    summary="Get official most valuable teams",
    operation_id="get_official_fpl_most_valuable_teams",
)
async def get_official_fpl_most_valuable_teams():
    results = await _official_call(scout.official_client.most_valuable_teams)
    return _official_collection("/stats/most-valuable-teams/", results)


@app.get(
    "/api/fpl/gameweeks/{gameweek}/winners",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Reference & Rankings"],
    summary="Get official gameweek winners",
    operation_id="get_official_fpl_event_winners",
)
async def get_official_fpl_event_winners(
    gameweek: int = Path(..., ge=1, le=38),
):
    result = await _official_call(scout.official_client.event_winners, gameweek)
    return _official_item(f"/winners/event/{gameweek}/", result)


@app.get(
    "/api/fpl/phases/{phase_id}/winners",
    response_model=OfficialFPLItemModel,
    tags=["Official FPL · Reference & Rankings"],
    summary="Get official phase winners",
    operation_id="get_official_fpl_phase_winners",
)
async def get_official_fpl_phase_winners(
    phase_id: int = Path(..., ge=1),
):
    result = await _official_call(scout.official_client.phase_winners, phase_id)
    return _official_item(f"/winners/phase/{phase_id}/", result)


async def _generate_scout_response(
    gameweek: Optional[int], public=False
) -> ResponseModel:
    try:
        predictions = await run_in_threadpool(scout.get_official_predictions, gameweek)
        team = await run_in_threadpool(scout.select_optimal_team, predictions)
        prediction_gameweek = int(predictions.attrs["gameweek"])
        if public:
            logger.info(
                "Generated public scout team for gameweek %d",
                prediction_gameweek,
            )
            return ResponseModel(
                scout_team=json.loads(team.to_json(orient="records")),
                player_points=[],
                gameweek=prediction_gameweek,
                version=config.get("version", "1.0.0"),
                source="official-fpl",
            )
        else:
            logger.info(
                "Generated public scout team for gameweek %d", prediction_gameweek
            )
            return ResponseModel(
                scout_team=json.loads(team.to_json(orient="records")),
                player_points=json.loads(predictions.to_json(orient="records")),
                gameweek=prediction_gameweek,
                version=config.get("version", "1.0.0"),
                source="official-fpl",
            )
    except OfficialFPLAPIError as error:
        logger.exception("Official FPL data request failed")
        raise HTTPException(status_code=502, detail=str(error)) from error
    except (InferenceError, ValueError) as error:
        logger.exception("Scout inference failed")
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get(
    "/api/scout",
    response_model=ResponseModel,
    tags=["Scout AI"],
    summary="Generate a public live scout squad",
    operation_id="generate_public_scout_team",
)
async def generate_public_scout_team(
    gameweek: Optional[int] = Query(None, ge=1, le=38),
):
    """Generate the web app's scout team from live official FPL data."""
    return await _generate_scout_response(gameweek, public=True)


@app.post(
    "/api/scout",
    response_model=ResponseModel,
    tags=["Scout AI"],
    summary="Generate an authenticated live scout squad",
    operation_id="generate_authenticated_scout_team",
)
async def generate_authenticated_scout_team(
    gameweek: Optional[int] = Query(None, ge=1, le=38),
    api_key: str = Depends(verify_api_key),
):
    """Generate a scout team without accepting third-party file uploads."""
    return await _generate_scout_response(gameweek, public=False)


@app.get(
    "/api/gw/scout",
    response_model=ResponseModel,
    tags=["Scout AI"],
    summary="Generate an authenticated gameweek squad",
    operation_id="get_gameweek_scout_team",
)
async def get_scout_team(
    gameweek: int = Query(..., ge=1, le=38),
    api_key: str = Depends(verify_api_key),
):
    """Generate a gameweek squad directly from official FPL data."""
    response = await _generate_scout_response(gameweek, public=False)
    response.player_points = []
    return response


@app.get(
    "/api/gw/playerpoints",
    response_model=ResponseModel,
    tags=["Scout AI"],
    summary="Generate and filter player projections",
    operation_id="get_gameweek_player_predictions",
)
async def get_player_predictions(
    params: PlayerPointsModel = Depends(),
    api_key: str = Depends(verify_api_key),
):
    """Generate official-data projections and apply optional player filters."""
    response = await _generate_scout_response(params.gameweek, public=False)
    filters = params.model_dump(exclude_unset=True)
    filters.pop("gameweek", None)

    def matches(player):
        for key, value in filters.items():
            if value is None:
                continue
            candidate = player.get(key)
            if key in {"web_name", "team_name"}:
                if str(candidate or "").casefold() != str(value).casefold():
                    return False
            elif key == "was_home":
                if bool(candidate) != value:
                    return False
            elif candidate != value:
                return False
        return True

    response.scout_team = []
    response.player_points = [
        player for player in response.player_points if matches(player)
    ]
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

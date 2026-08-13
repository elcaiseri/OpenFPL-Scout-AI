"""OpenFPL Scout API backed exclusively by official FPL runtime data."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

import aiofiles
from fastapi import Depends, FastAPI, HTTPException, Query
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
                        "bearer"
                        if _uses_bearer_auth(route.dependant)
                        else "public"
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
        "documentation": {
            "swagger": "/docs",
            "redoc": "/redoc",
            "openapi": "/openapi.json",
        },
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


def _official_collection(endpoint: str, results) -> OfficialFPLCollectionModel:
    return OfficialFPLCollectionModel(
        official_endpoint=endpoint,
        count=len(results),
        results=results,
    )


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
):
    results = await _official_call(
        scout.official_client.mapped_players,
        team_id,
        element_type,
        selectable_only,
    )
    return _official_collection("/bootstrap-static/", results)


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
    return OfficialFPLItemModel(
        official_endpoint="/bootstrap-static/", data=result
    )


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
    return OfficialFPLItemModel(
        official_endpoint=f"/element-summary/{player_id}/", data=result
    )


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
):
    results = await _official_call(
        scout.official_client.mapped_fixtures, gameweek, team_id
    )
    return _official_collection("/fixtures/", results)


async def _generate_scout_response(gameweek: Optional[int]) -> ResponseModel:
    try:
        predictions = await run_in_threadpool(
            scout.get_official_predictions, gameweek
        )
        team = await run_in_threadpool(scout.select_optimal_team, predictions)
        prediction_gameweek = int(predictions.attrs["gameweek"])
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
    return await _generate_scout_response(gameweek)


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
    return await _generate_scout_response(gameweek)


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
    response = await _generate_scout_response(gameweek)
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
    response = await _generate_scout_response(params.gameweek)
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

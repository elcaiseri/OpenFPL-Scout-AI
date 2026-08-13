"""OpenFPL Scout API backed exclusively by official FPL runtime data."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Optional

import aiofiles
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from src.auth import verify_api_key
from src.logger import get_logger
from src.models import PlayerPointsModel, ResponseModel
from src.official_fpl import OfficialFPLAPIError
from src.scout import FPLScout, InferenceError
from src.utils import load_config

logger = get_logger(__name__)

config = load_config("config/config.yaml")
scout: FPLScout


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
)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the OpenFPL web application."""
    try:
        async with aiofiles.open("static/index.html", "r") as file:
            return HTMLResponse(content=await file.read())
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="static/index.html not found") from error
    except OSError as error:
        logger.exception("Failed to read index.html")
        raise HTTPException(status_code=500, detail="Failed to read index.html") from error


@app.get("/api")
async def get_api_info(api_key: str = Depends(verify_api_key)):
    """Describe authenticated API endpoints."""
    return {
        "message": "OpenFPL — Official FPL data, AI squad projections",
        "version": config.get("version", "1.0.0"),
        "source": "https://fantasy.premierleague.com/api/",
        "endpoints": {
            "/api/scout": "GET/POST - Generate a live official-data scout team",
            "/api/gw/scout": "GET - Generate a scout team for a gameweek",
            "/api/gw/playerpoints": "GET - Generate/filter player projections",
            "/api/gameweeks": "GET - Official available gameweek state",
        },
    }


@app.get("/api/health")
async def check_health():
    """Return service and configured data-source status."""
    return {
        "status": "healthy",
        "source": "official-fpl",
        "models": len(scout.model_artifacts),
    }


@app.get("/api/gameweeks")
async def get_available_gameweeks():
    """Return gameweeks exposed by the official FPL event state."""
    try:
        return await run_in_threadpool(scout.official_client.available_gameweeks)
    except OfficialFPLAPIError as error:
        logger.exception("Failed to load official gameweeks")
        raise HTTPException(status_code=502, detail=str(error)) from error


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


@app.get("/api/scout", response_model=ResponseModel)
async def generate_public_scout_team(
    gameweek: Optional[int] = Query(None, ge=1, le=38),
):
    """Generate the web app's scout team from live official FPL data."""
    return await _generate_scout_response(gameweek)


@app.post("/api/scout", response_model=ResponseModel)
async def generate_authenticated_scout_team(
    gameweek: Optional[int] = Query(None, ge=1, le=38),
    api_key: str = Depends(verify_api_key),
):
    """Generate a scout team without accepting third-party file uploads."""
    return await _generate_scout_response(gameweek)


@app.get("/api/gw/scout", response_model=ResponseModel)
async def get_scout_team(
    gameweek: int = Query(..., ge=1, le=38),
    api_key: str = Depends(verify_api_key),
):
    """Generate a gameweek squad directly from official FPL data."""
    response = await _generate_scout_response(gameweek)
    response.player_points = []
    return response


@app.get("/api/gw/playerpoints", response_model=ResponseModel)
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

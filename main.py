import json
import os
import shutil
from contextlib import asynccontextmanager
from tempfile import NamedTemporaryFile
from typing import Optional

import aiofiles
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_redoc_html

from src.auth import verify_api_key
from src.logger import get_logger
from src.models import PlayerPointsModel, ResponseModel
from src.scout import FPLScout
from src.utils import load_config, save_scout_team_to_json

logger = get_logger(__name__)

config = {}
scout: FPLScout


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, scout
    logger.info("Initializing application")
    config = load_config("config/config.yaml")
    scout = FPLScout(config)
    logger.info("FPLScout initialized and ready.")
    yield
    logger.info("Shutting down application.")


# Initialize FastAPI with enhanced branding
app = FastAPI(
    title="OpenFPL Scout AI - Professional FPL Analytics API",
    description="""
## 🚀 AI-Powered Fantasy Premier League Intelligence Platform

**OpenFPL Scout AI** is a professional-grade REST API that leverages ensemble machine learning to deliver cutting-edge Fantasy Premier League analytics and team optimization.

### 🎯 Key Features

- **🤖 Ensemble ML Models**: Combines XGBoost, CatBoost, and Linear Regression for superior prediction accuracy
- **⚡ Real-time Analytics**: Access live player predictions and gameweek analytics
- **🏆 Team Optimization**: Automated optimal team selection using advanced algorithms
- **📊 Comprehensive Data**: Historical performance, fixture difficulty, and expected points
- **🔒 Secure & Reliable**: Production-ready API with authentication and error handling
- **📱 Developer Friendly**: Clear documentation, consistent responses, and easy integration

### 💡 Use Cases

1. **FPL Managers**: Get AI-powered team recommendations to maximize your weekly points
2. **Data Analysts**: Access rich player performance data for custom analysis
3. **App Developers**: Integrate FPL predictions into your fantasy football applications
4. **Researchers**: Study player performance patterns using machine learning insights

### 🔥 Quick Start

Visit our [RapidAPI Marketplace](https://rapidapi.com/elcaiseri-elcaiseri-default/api/openfpl-api) for instant API access with free tier available.

### 📚 Main Endpoints

- `GET /api/gw/scout` - Get optimal FPL team for specific gameweek
- `GET /api/gw/playerpoints` - Get filtered player point predictions
- `GET /api/gameweeks` - List all available gameweeks with saved data
- `GET /api/health` - Health check endpoint

### 🏅 About

Developed by [@elcaiseri](https://github.com/elcaiseri) | [GitHub Repository](https://github.com/elcaiseri/OpenFPL-Scout-AI)
    """,
    version=config.get("version", "1.0.0"),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,  # We'll create a custom redoc endpoint
    contact={
        "name": "OpenFPL Scout AI Support",
        "url": "https://github.com/elcaiseri/OpenFPL-Scout-AI",
        "email": "iqasem4444@gmail.com",
    },
    license_info={
        "name": "MIT License",
        "url": "https://github.com/elcaiseri/OpenFPL-Scout-AI/blob/main/LICENSE",
    },
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")
app.mount(
    "/data/internal/scout_team",
    StaticFiles(directory="data/internal/scout_team"),
    name="scout-team-data",
)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve main UI."""
    try:
        async with aiofiles.open("static/index.html", "r") as f:
            content = await f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="static/index.html not found")
    except Exception as e:
        logger.error(f"Failed to read index.html: {e}")
        raise HTTPException(status_code=500, detail="Failed to read index.html")

    return HTMLResponse(content=content)


@app.get("/redoc", response_class=HTMLResponse, include_in_schema=False)
async def serve_custom_redoc():
    """Serve custom branded ReDoc documentation."""
    try:
        async with aiofiles.open("static/redoc.html", "r") as f:
            content = await f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="static/redoc.html not found")
    except Exception as e:
        logger.error(f"Failed to read redoc.html: {e}")
        raise HTTPException(status_code=500, detail="Failed to read redoc.html")

    return HTMLResponse(content=content)


@app.get("/api")
async def get_api_info(api_key: str = Depends(verify_api_key)):
    """Get API information."""
    return {
        "message": "OpenFPL - AI Fantasy Premier League Scout",
        "version": config.get("version", "1.0.0"),
        "endpoints": {
            "/api/scout": "POST - Generate scout team from uploaded CSV",
            "/api/gw/scout": "GET - Get saved scout team for gameweek",
            "/api/gw/playerpoints": "GET - Get player predictions for gameweek",
            "/api/gameweeks": "GET - List all available gameweeks",
        },
    }


@app.get("/api/health")
async def check_health(api_key: str = Depends(verify_api_key)):
    """Health check."""
    return {"status": "healthy"}


@app.get("/api/gameweeks")
async def get_available_gameweeks():
    """Get list of available gameweeks."""
    try:
        scout_data_dir = "data/internal/scout_team"
        gameweeks = []

        if os.path.exists(scout_data_dir):
            for filename in os.listdir(scout_data_dir):
                if filename.startswith("gw_") and filename.endswith(".json"):
                    try:
                        gw_num = int(filename.replace("gw_", "").replace(".json", ""))
                        gameweeks.append(gw_num)
                    except ValueError:
                        continue

        gameweeks.sort()
        return {
            "gameweeks": gameweeks,
            "total": len(gameweeks),
            "latest": max(gameweeks) if gameweeks else None,
        }
    except Exception as e:
        logger.error(f"Failed to get gameweeks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/scout", response_model=ResponseModel)
async def generate_scout_team(
    file: UploadFile = File(...),
    gameweek: Optional[int] = Query(None, ge=1, le=38),
    api_key: str = Depends(verify_api_key),
):
    """Generate scout team from uploaded CSV."""
    tmp_path = None
    try:
        # Save uploaded file temporarily
        with NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name

        # Get or generate predictions
        predictions = scout.get_player_predictions(tmp_path, gameweek=gameweek)

        # Select team
        team = scout.select_optimal_team(predictions)

        # Build response
        response = ResponseModel(
            scout_team=json.loads(team.to_json(orient="records")),
            player_points=json.loads(predictions.to_json(orient="records")),
            gameweek=scout.gameweek,
            version=config.get("version", "1.0.0"),
        )

        save_scout_team_to_json(response, scout.gameweek)
        return response

    except Exception as e:
        logger.error(f"Failed to generate scout team: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.get("/api/gw/scout", response_model=ResponseModel)
async def get_scout_team(gameweek: int, api_key: str = Depends(verify_api_key)):
    """Get saved scout team for gameweek."""

    path = f"data/internal/scout_team/gw_{gameweek}.json"

    try:
        async with aiofiles.open(path, "r") as f:
            content = await f.read()
            payload = json.loads(content)

        return ResponseModel(
            scout_team=payload.get("scout_team", []),
            player_points=[],
            gameweek=gameweek,
            version=payload.get("version", "1.0.0"),
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Data not found for gameweek {gameweek}"
        )
    except Exception as e:
        logger.error(f"Failed to get scout team: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gw/playerpoints", response_model=ResponseModel)
async def get_player_predictions(
    params: PlayerPointsModel = Depends(), api_key: str = Depends(verify_api_key)
):
    """Get player predictions for gameweek with optional filters."""
    path = f"data/internal/scout_team/gw_{params.gameweek}.json"

    try:
        async with aiofiles.open(path, "r") as f:
            content = await f.read()
            payload = json.loads(content)

        players = payload.get("player_points", [])

        # Apply filters
        filters = params.dict(exclude_unset=True)
        filters.pop("gameweek", None)

        def matches(player):
            for k, v in filters.items():
                if k == "element_type":
                    if v is not None and player.get("element_type") != v:
                        return False
                elif k == "web_name":
                    if (
                        v is not None
                        and str(player.get("web_name", "")).lower() != str(v).lower()
                    ):
                        return False
                elif k == "team_name":
                    if (
                        v is not None
                        and str(player.get("team_name", "")).lower() != str(v).lower()
                    ):
                        return False
                elif k == "was_home":
                    if v is not None and bool(player.get("was_home")) != v:
                        return False
            return True

        if filters:
            players = [p for p in players if matches(p)]

        return ResponseModel(
            scout_team=[],
            player_points=players,
            gameweek=params.gameweek,
            version=payload.get("version", "1.0.0"),
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"Data not found for gameweek {params.gameweek}"
        )
    except Exception as e:
        logger.error(f"Failed to get player predictions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

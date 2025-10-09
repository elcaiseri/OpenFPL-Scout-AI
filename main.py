import json
from fastapi import FastAPI, HTTPException, Depends
from fastapi import File, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import status

from src.utils import load_config, save_scout_team_to_json
from src.logger import get_logger
from src.scout import FPLScout
from src.models import ResponseModel, PlayerPointsModel
from src.auth import verify_api_key

from tempfile import NamedTemporaryFile
import shutil
import os

logger = get_logger(__name__)

# Load configuration and model paths
logger.info("Initializing application: loading configuration from config/config.yaml")
config = load_config('config/config.yaml')

# --- App Initialization ---
app = FastAPI(
    title="OpenFPL API",
    description="AI-powered Fantasy Premier League Scout API for optimal team selection and player predictions",
    version=config.get('version', '1.0.0'),
)

# Mount static directories
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")
app.mount("/data/internal/scout_team", StaticFiles(directory="data/internal/scout_team"), name="scout-team-data")

# Initialize predictions cache
app.state.predictions_cache = {}

# --- Endpoints ---
@app.get("/", tags=["Root"], summary="Serve main UI", response_class=HTMLResponse)
async def serve_index():
    """
    Serve the main HTML page for the OpenFPL web interface.
    
    Returns:
        HTMLResponse: The main index.html page
    """
    logger.info("Serving main index page")
    with open("static/index.html") as f:
        html_content = f.read()

    return HTMLResponse(content=html_content, status_code=200, media_type="text/html")

@app.get("/api", tags=["API"], summary="Get API information")
async def get_api_info(api_key: str = Depends(verify_api_key)):
    """
    Get basic API information including version, documentation links, and endpoint usage.

    Returns:
        dict: API metadata including version, documentation URL, credits, and endpoint usage info
    """
    logger.info("API information endpoint accessed")
    return {
        "message": "OpenFPL - AI Fantasy Premier League Scout",
        "version": config.get('version', '1.0.0'),
        "credits": "Developed by Kassem@elcaiseri, 2025",
        "usage": {
            "/api/gw/scout": {
                "description": "Retrieve the scout team -only- points for a specific gameweek.",
                "method": "GET",
                "params": {
                    "gameweek": "int (optional, defaults to latest available)"
                },
                "example": "/api/gw/scout?gameweek=1"
            },
            "/api/gw/playerpoints": {
                "description": "Retrieve all +700 players point predictions for a gameweek, with optional filters.",
                "method": "GET",
                "params": {
                    "gameweek": "int (optional, defaults to latest available)",
                    "element_type": "int (optional, position filter)",
                    "web_name": "str (optional, player name filter)",
                    "team_name": "str (optional, team name filter)",
                    "was_home": "bool (optional, home/away filter)"
                },
                "example": "/api/gw/playerpoints?gameweek=1&element_type=3"
            },
            "/api/gameweeks": {
                "description": "List all available gameweeks with scout team data.",
                "method": "GET",
                "params": {},
                "example": "/api/gameweeks"
            },
        }
    }

@app.get("/api/health", tags=["Health Check"], summary="Check API health status")
async def check_health(api_key: str = Depends(verify_api_key)):
    """
    Health check endpoint to verify the API is running.
    
    Returns:
        dict: Health status indicator
    """
    logger.info("Health check endpoint accessed - service is healthy")
    return {"status": "healthy"}

@app.get("/api/gameweeks", tags=["Data"], summary="Get available gameweeks")
async def get_available_gameweeks():
    """
    Retrieve a list of all available gameweeks with scout team data.
    
    Returns:
        dict: Contains list of gameweeks, total count, and latest gameweek number
        
    Raises:
        HTTPException: If unable to read gameweek data directory
    """
    logger.info("Fetching available gameweeks from scout team data directory")
    try:
        scout_data_dir = "data/internal/scout_team"
        available_gameweeks = []

        if os.path.exists(scout_data_dir):
            for filename in os.listdir(scout_data_dir):
                if filename.startswith("gw_") and filename.endswith(".json"):
                    try:
                        # Extract gameweek number from filename like "gw_1.json"
                        gw_num = int(filename.replace("gw_", "").replace(".json", ""))
                        available_gameweeks.append(gw_num)
                    except ValueError:
                        logger.warning(f"Skipping invalid gameweek filename: {filename}")
                        continue

        available_gameweeks.sort()
        logger.info(f"Successfully retrieved {len(available_gameweeks)} available gameweeks: {available_gameweeks}")

        return {
            "gameweeks": available_gameweeks,
            "total": len(available_gameweeks),
            "latest": max(available_gameweeks) if available_gameweeks else None
        }
    except Exception as e:
        logger.error(f"Failed to retrieve available gameweeks: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Unable to retrieve available gameweeks: {str(e)}"
        )

@app.post("/api/scout", response_model=ResponseModel, tags=["Scout"], summary="Generate scout team from uploaded data")
async def generate_scout_team(file: UploadFile = File(...), api_key: str = Depends(verify_api_key)):
    """
    Generate an optimal FPL scout team based on uploaded player data CSV file.
    
    Args:
        file: CSV file containing player data for the current gameweek
        api_key: API authentication key
        
    Returns:
        ResponseModel: Contains scout team, player predictions, and metadata
        
    Raises:
        HTTPException: If file processing or team generation fails
    """
    logger.info(f"Scout team generation initiated with uploaded file: {file.filename}")
    cache = app.state.predictions_cache
    tmp_path = None

    try:
        # Save uploaded file to a temporary location
        logger.debug(f"Saving uploaded file {file.filename} to temporary location")
        with NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name
        logger.debug(f"File  to temporary path: {tmp_path}")

        scout = FPLScout(config, tmp_path)
        logger.info(f"FPLScout initialized for gameweek {scout.gameweek}")

        if scout.gameweek in cache:
            logger.info(f"Using cached player predictions for gameweek {scout.gameweek}")
            player_predictions_df = cache[scout.gameweek]
        else:
            logger.info(f"Generating new player predictions for gameweek {scout.gameweek}")
            player_predictions_df = scout.get_player_predictions()
            cache[scout.gameweek] = player_predictions_df
            logger.debug(f"Predictions cached for gameweek {scout.gameweek}")

        logger.info("Selecting optimal team based on player predictions")
        scout_team = scout.select_optimal_team(player_predictions_df)

        response = ResponseModel(
            scout_team=scout_team,
            player_points=json.loads(player_predictions_df.to_json(orient="records")),
            gameweek=scout.gameweek,
            version=config.get('version', '1.0.0'),
        )
        
        save_scout_team_to_json(response, scout.gameweek)
        logger.info(f"Scout team successfully generated and  for gameweek {scout.gameweek}")

        return response
    except Exception as e:
        logger.error(f"Failed to generate scout team: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Scout team generation failed: {str(e)}"
        )
    finally:
        # Ensure temporary file is deleted
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.debug(f"Temporary file {tmp_path} successfully deleted")
            except Exception as cleanup_error:
                logger.warning(f"Failed to delete temporary file {tmp_path}: {str(cleanup_error)}")

@app.get("/api/gw/scout", response_model=ResponseModel, tags=["Scout"], summary="Get  scout team")
async def get_scout_team(gameweek: int, api_key: str = Depends(verify_api_key)):
    """
    Retrieve a previously  scout team for a specific gameweek.
    
    Args:
        gameweek: The gameweek number to retrieve
        api_key: API authentication key
        
    Returns:
        ResponseModel: Contains  scout team and metadata
        
    Raises:
        HTTPException: If gameweek data file is not found or invalid
    """
    logger.info(f"Retrieving  scout team for gameweek {gameweek}")

    path = os.path.join("data", "internal", "scout_team", f"gw_{gameweek}.json")

    try:
        logger.debug(f"Reading scout team data from: {path}")
        with open(path, "r") as f:
            payload = json.load(f)

        if not isinstance(payload, dict) or payload.get("gameweek") != gameweek:
            logger.error(f"Invalid payload structure or gameweek mismatch for GW {gameweek}")
            raise ValueError("Invalid  payload or mismatched gameweek")

        logger.info(f"Successfully retrieved scout team for gameweek {gameweek}")
        return ResponseModel(
            scout_team=payload.get("scout_team", []),
            player_points=[],
            gameweek=gameweek,
            version=payload.get("version", '1.0.0'),
        )

    except FileNotFoundError:
        logger.error(f"Scout team data file not found for gameweek {gameweek}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scout team data not found for gameweek {gameweek}",
        )
    except Exception as e:
        logger.error(f"Failed to retrieve scout team for gameweek {gameweek}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve  scout team: {str(e)}",
        )


@app.get("/api/gw/playerpoints", response_model=ResponseModel, tags=["Scout"], summary="Get player predictions")
async def get_player_predictions(params: PlayerPointsModel = Depends(), api_key: str = Depends(verify_api_key)):
    """
    Retrieve player point predictions for a specific gameweek, optionally filtered by parameters.

    Args:
        params (PlayerPointsModel): Filter parameters (gameweek required, plus optional position, team, etc.)
        api_key (str): API authentication key

    Returns:
        ResponseModel: Contains filtered player predictions and metadata

    Raises:
        HTTPException: If gameweek data file is not found or invalid
    """
    gameweek = params.gameweek
    logger.info(f"Retrieving player predictions for gameweek {gameweek} with filters: {params.dict()}")

    path = os.path.join("data", "internal", "scout_team", f"gw_{gameweek}.json")

    try:
        logger.debug(f"Reading player predictions from: {path}")
        with open(path, "r") as f:
            payload = json.load(f)

        if not isinstance(payload, dict) or payload.get("gameweek") != gameweek:
            logger.error(f"Invalid payload structure or gameweek mismatch for GW {gameweek}")
            raise ValueError("Invalid payload or mismatched gameweek")

        player_points = payload.get("player_points", [])

        # Filter player_points by params if provided
        filters = params.dict(exclude_unset=True)
        filters.pop("gameweek", None)  # Already used

        def matches(player):
            for k, v in filters.items():
                if k == "element_type":
                    if v is not None and player.get("element_type") != v:
                        return False
                elif k == "web_name":
                    if v is not None and str(player.get("web_name", "")).lower() != str(v).lower():
                        return False
                elif k == "team_name":
                    if v is not None and str(player.get("team_name", "")).lower() != str(v).lower():
                        return False
                elif k == "was_home":
                    if v is not None and bool(player.get("was_home")) != v:
                        return False
            return True

        if filters:
            player_points = [p for p in player_points if matches(p)]

        logger.info(f"Successfully retrieved {len(player_points)} filtered player predictions for gameweek {gameweek}")
        return ResponseModel(
            scout_team=[],
            player_points=player_points,
            gameweek=gameweek,
            version=payload.get("version", '1.0.0'),
        )

    except FileNotFoundError:
        logger.error(f"Player predictions file not found for gameweek {gameweek}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player predictions not found for gameweek {gameweek}",
        )
    except Exception as e:
        logger.error(f"Failed to retrieve player predictions for gameweek {gameweek}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve player predictions: {str(e)}",
        )

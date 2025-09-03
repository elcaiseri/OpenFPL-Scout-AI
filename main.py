import json
from fastapi import FastAPI, HTTPException, Depends
from fastapi import File, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import status

from src.utils import load_config, save_scout_team_to_json, should_mount_static_files, get_environment_type, get_static_files_strategy
from src.logger import get_logger
from src.scout import FPLScout
from src.models import ResponseModel
from src.auth import verify_api_key

from tempfile import NamedTemporaryFile
import shutil
import os

logger = get_logger(__name__)

# Load configuration and model paths
logger.info("Loading configuration and model paths...")
config = load_config('config/config.yaml')

# --- App Initialization ---
app = FastAPI(
    title="OpenFPL API",
    description="AI-powered Fantasy Premier League Scout API",
    version=config.get('version', '1.0.0'),
)

# Environment and static files configuration
env_type = get_environment_type(config)
static_strategy = get_static_files_strategy(config)

logger.info(f"Environment: {env_type}, Static files strategy: {static_strategy}")

# Conditionally mount static files
if should_mount_static_files(config):
    # Mount static files for local development or when strategy is 'local'
    static_dirs = config.get("static_files", {}).get("local", {}).get("directories", [
        {"name": "static", "path": "static", "mount_path": "/static"},
        {"name": "assets", "path": "assets", "mount_path": "/assets"},
        {"name": "data", "path": "data", "mount_path": "/data"}
    ])
    
    for static_dir in static_dirs:
        dir_path = static_dir["path"]
        mount_path = static_dir["mount_path"]
        name = static_dir["name"]
        
        # Only mount if directory exists
        if os.path.exists(dir_path):
            app.mount(mount_path, StaticFiles(directory=dir_path), name=name)
            logger.info(f"Mounted static directory: {dir_path} -> {mount_path}")
        else:
            logger.warning(f"Static directory not found, skipping: {dir_path}")
else:
    logger.info("Static files mounting skipped (production mode with non-local strategy)")

app.state.predictions_cache = {}

# --- Endpoints ---
@app.get("/", tags=["Root"])
async def root():
    """Serve the main HTML page."""
    with open("static/index.html") as f:
        html_content = f.read()

    return HTMLResponse(content=html_content, status_code=200, media_type="text/html")

@app.get("/api", tags=["API"])
async def api_root(api_key: str = Depends(verify_api_key)):
    logger.info("API root endpoint called")
    return {
        "message": "OpenFPL - AI Fantasy Premier League Scout",
        "version": config.get('version', '1.0.0'),
        "documentation": "/docs",
        "credits": "Developed by Kassem@elcaiseri, 2025",
    }

@app.get("/api/health", tags=["Health Check"])
async def health_check(api_key: str = Depends(verify_api_key)):
    logger.info("Health check endpoint called")
    return {"status": "healthy"}

@app.post("/api/scout", response_model=ResponseModel, tags=["Scout"])
async def get_scout_team(file: UploadFile = File(...), api_key: str = Depends(verify_api_key)):
    logger.info("Scout team endpoint (with upload) called")
    cache = app.state.predictions_cache
    tmp_path = None

    try:
        # Save uploaded file to a temporary location
        with NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name

        scout = FPLScout(config, tmp_path)

        if scout.gameweek in cache:
            logger.info(f"Using cached predictions for gameweek {scout.gameweek}")
            player_predictions_df = cache[scout.gameweek]
        else:
            player_predictions_df = scout.get_player_predictions()
            cache[scout.gameweek] = player_predictions_df

        scout_team = scout.select_optimal_team(player_predictions_df)

        response = ResponseModel(
            scout_team=scout_team,
            player_points=json.loads(player_predictions_df.to_json(orient="records")),
            gameweek=scout.gameweek,
            version=config.get('version', '1.0.0'),

        )
        save_scout_team_to_json(response, scout.gameweek)

        return response
    except Exception as e:
        logger.error(f"Error in /scout endpoint: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        # Ensure temporary file is deleted
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.info(f"Temporary file {tmp_path} deleted.")
            except Exception as cleanup_error:
                logger.warning(f"Failed to delete temporary file {tmp_path}: {cleanup_error}")

from fastapi import FastAPI, HTTPException, Path

import pandas as pd

from src.utils import load_config
from src.logger import get_logger
from src.scout import FPLScout
from src.models import ResponseModel
from fastapi import File, UploadFile
from tempfile import NamedTemporaryFile
import shutil
from fastapi import status
import os

logger = get_logger(__name__)

# --- App Initialization ---
app = FastAPI(
    title="OpenFPL API",
    description="AI-powered Fantasy Premier League Scout API",
    version="1.0.0"
)

app.state.predictions_cache = {}

# Load configuration and model paths
logger.info("Loading configuration and model paths...")
config = load_config('config/config.yaml')
data_path = 'data/external/fpl-data-stats-2.csv'

# --- Endpoints ---

@app.get("/")
async def root():
    logger.info("Root endpoint called")
    return {
        "message": "OpenFPL - AI Fantasy Premier League Scout",
        "version": "1.0.0",
        "credits": "Developed by Kassem@elcaiseri, 2025",
        "documentation": "/docs"
    }

@app.get("/health", tags=["Health Check"])
async def health_check():
    logger.info("Health check endpoint called")
    return {"status": "healthy"}

@app.post("/scout", response_model=ResponseModel, tags=["Scout"])
async def get_scout_team(file: UploadFile = File(...)):
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
            player_predictions_df = await scout.get_player_predictions()
            cache[scout.gameweek] = player_predictions_df

        scout_team = scout.select_optimal_team(player_predictions_df)

        return ResponseModel(
            scout_team=scout_team,
            player_points=[],
            gameweek=scout.gameweek
        )
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

from fastapi import FastAPI, HTTPException, Path

import pandas as pd
import uvicorn

from src.utils import load_config
from src.logger import get_logger
from src.scout import FPLScout
from src.models import ResponseModel

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
model_paths = [meta['path'] for meta in config['models'].values()]
logger.info(f"Loaded model paths: {model_paths}")

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

@app.get("/scout-team", response_model=ResponseModel, tags=["Scout Team"])
async def get_scout_team():
    """Get optimal team for current gameweek"""
    logger.info("Scout team endpoint called")
    cache = app.state.predictions_cache
    scout = FPLScout(model_paths, data_path, gameweek=None, cached=cache)
    gameweek = scout.gameweek

    try:
        # Use cached predictions if available
        if gameweek in cache:
            logger.info(f"Using cached predictions for gameweek {gameweek}")
            player_predictions = cache[gameweek]
        else:
            logger.info(f"Generating predictions for gameweek {gameweek}")
            player_predictions = await scout.get_player_predictions()
            cache[gameweek] = player_predictions

        # Ensure player_predictions is a DataFrame
        df_predictions = (
            player_predictions
            if isinstance(player_predictions, pd.DataFrame)
            else pd.DataFrame(player_predictions)
        )

        logger.info("Selecting optimal team")
        optimal_team = scout.select_optimal_team(df_predictions)

        # Ensure optimal_team is serializable
        if hasattr(optimal_team, "to_dict"):
            optimal_team = optimal_team.to_dict('records')

        logger.info("Optimal team generated successfully")
        return ResponseModel(content=optimal_team)
    except Exception as e:
        logger.error(f"Error generating scout team: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error generating scout team: {str(e)}"
        )

@app.get("/scout-report", response_model=ResponseModel, tags=["Scout Report"])
async def get_scout_report():
    """Get all player predictions for current gameweek"""
    logger.info("Scout report endpoint called")
    cache = app.state.predictions_cache
    scout = FPLScout(model_paths, data_path, gameweek=None, cached=cache)
    gameweek = scout.gameweek

    if gameweek in cache:
        logger.info(f"Using cached predictions for gameweek {gameweek}")
        predictions = cache[gameweek]
        if hasattr(predictions, "to_dict"):
            predictions = predictions.sort_values(by="expected_points", ascending=False).to_dict('records')
        return ResponseModel(content=predictions)

    try:
        logger.info(f"Generating predictions for gameweek {gameweek}")
        player_predictions = await scout.get_player_predictions()
        cache[gameweek] = player_predictions

        # Ensure predictions are serializable
        if hasattr(player_predictions, "to_dict"):
            predictions_data = player_predictions.to_dict('records')
        else:
            predictions_data = player_predictions

        logger.info("Scout report generated successfully")
        return ResponseModel(content=predictions_data)
    except Exception as e:
        logger.error(f"Error getting scout report: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error getting scout report: {str(e)}"
        )

if __name__ == "__main__":
    logger.info("Starting OpenFPL API server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

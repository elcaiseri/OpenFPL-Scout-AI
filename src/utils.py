import pandas as pd
import yaml
import os
import requests
from typing import Dict
import json

def load_config(config_path):
    """Load configuration settings from a YAML file."""
    try:
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)
        return config
    except (FileNotFoundError, yaml.YAMLError, Exception):
        return {}

def get_environment_type(config: dict) -> str:
    """Get the environment type from config or environment variable."""
    # Check environment variable first, then config, then default
    env_type = os.getenv("ENVIRONMENT", 
                        config.get("environment", {}).get("type", "development"))
    return env_type.lower()

def get_static_files_strategy(config: dict) -> str:
    """Get the static files strategy from config or environment variable."""
    # Check environment variable first, then config, then default
    strategy = os.getenv("STATIC_FILES_STRATEGY",
                        config.get("static_files", {}).get("strategy", "local"))
    return strategy.lower()

def should_mount_static_files(config: dict) -> bool:
    """Determine if static files should be mounted based on environment and strategy."""
    env_type = get_environment_type(config)
    strategy = get_static_files_strategy(config)
    
    # Mount static files if:
    # 1. Environment is development, OR
    # 2. Strategy is local regardless of environment
    return env_type == "development" or strategy == "local"

def get_static_file_url(config: dict, mount_path: str, filename: str) -> str:
    """Generate the appropriate URL for a static file based on the current strategy."""
    strategy = get_static_files_strategy(config)
    
    if strategy == "local" or should_mount_static_files(config):
        # Use local mounted paths
        return f"{mount_path.rstrip('/')}/{filename}"
    elif strategy == "gcs":
        # Use GCS bucket URL
        gcs_config = config.get("static_files", {}).get("gcs", {})
        bucket_name = gcs_config.get("bucket_name", "")
        base_url = gcs_config.get("base_url", "")
        
        if base_url:
            return f"{base_url.rstrip('/')}/{filename}"
        elif bucket_name:
            return f"https://storage.googleapis.com/{bucket_name}{mount_path.rstrip('/')}/{filename}"
        else:
            # Fallback to local path if GCS not configured
            return f"{mount_path.rstrip('/')}/{filename}"
    elif strategy == "cdn":
        # Use CDN URL
        cdn_config = config.get("static_files", {}).get("cdn", {})
        base_url = cdn_config.get("base_url", "")
        
        if base_url:
            return f"{base_url.rstrip('/')}{mount_path.rstrip('/')}/{filename}"
        else:
            # Fallback to local path if CDN not configured
            return f"{mount_path.rstrip('/')}/{filename}"
    else:
        # Default to local path
        return f"{mount_path.rstrip('/')}/{filename}"

def get_next_gameweek(data) -> int:
    """Determine the next gameweek number based on loaded data."""
    if data.empty or "gameweek" not in data.columns:
        return 1
    next_gw = int(data["gameweek"].max()) + 1
    if not (1 <= next_gw <= 38):
        next_gw = 1
    return next_gw


def fetch_gw_match_data(gameweek: int, team_mapping: dict = None) -> Dict[str, dict]:
    """Fetch Premier League match data for the given gameweek and return a mapping of team to opponent and match info."""
    api_url = "https://api.football-data.org/v4/competitions/PL/matches"
    api_key = os.getenv("FPL_API_KEY", "")
    if not api_key:
        raise ValueError("API key for football-data.org is not set. Please set the FPL_API_KEY environment variable.")

    headers = {
        "X-Auth-Token": api_key
    }
    params = {"matchday": gameweek}

    response = requests.get(api_url, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    data = response.json()

    matches = []
    for match in data.get("matches", []):
        matches.append({
            "team_name": match["homeTeam"]["name"],
            "opponent_team_name": match["awayTeam"]["name"],
            "utcDate": match["utcDate"],
            "status": match["status"],
            "gameweek": match["season"].get("currentMatchday", gameweek),
            "was_home": True
        })
        matches.append({
            "team_name": match["awayTeam"]["name"],
            "opponent_team_name": match["homeTeam"]["name"],
            "utcDate": match["utcDate"],
            "status": match["status"],
            "gameweek": match["season"].get("currentMatchday", gameweek),
            "was_home": False
        })

    df = pd.DataFrame(matches)
    if team_mapping:
        df[["team_name", "opponent_team_name"]] = df[["team_name", "opponent_team_name"]].replace(team_mapping)

    df.set_index("team_name", inplace=True)
    match_dict = df.to_dict(orient="index")
    return match_dict

def save_scout_team_to_json(scout_team, gameweek: int):
    """Save scout team data to a JSON file if it doesn't already exist."""
    scout_team_json = scout_team.model_dump()
    save_dir = "data/internal/scout_team"
    os.makedirs(save_dir, exist_ok=True)
    file_path = f"{save_dir}/gw_{gameweek}.json"
    # save gcp storage I/O api cost
    if not os.path.exists(file_path):
        with open(file_path, "w") as json_file:
            json.dump(scout_team_json, json_file, indent=4)
    # else do nothing

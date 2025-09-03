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

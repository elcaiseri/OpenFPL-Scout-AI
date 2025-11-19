import warnings
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
import pandas as pd

from src.logger import get_logger
from src.utils import fetch_gw_match_data, load_config

warnings.filterwarnings("ignore")

logger = get_logger(__name__)


class FPLScout:
    """Fantasy Premier League player scout for prediction and team selection."""

    POSITION_MAPPING: Dict[int, str] = {
        1: "Goalkeeper",
        2: "Defender",
        3: "Midfielder",
        4: "Forward",
    }
    TEAM_SELECTION: Dict[int, int] = {1: 2, 2: 5, 3: 5, 4: 3}  # Players per position

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        logger.info("Initializing FPLScout...")
        self.models: List[Any] = [
            joblib.load(meta["path"]) for meta in config["models"].values()
        ]
        self.gameweek = None
        logger.info(f"Loaded {len(self.models)} models")

    def get_player_predictions(
        self, data_path: str, gameweek: Optional[int] = None
    ) -> pd.DataFrame:
        """Generate predictions for all players."""
        # Load and prepare data
        logger.info(f"Loading data from {data_path}")
        data = pd.read_csv(data_path)
        logger.info(f"Loaded {len(data)} records")

        self.gameweek = gameweek or int(data["gameweek"].max()) + 1
        data = data[data.gameweek < self.gameweek] if gameweek else data
        logger.info(f"Filtered to {len(data)} records before gameweek {self.gameweek}")

        # Add missing numerical columns and fill with 0
        for col in self.config["numerical_columns"]:
            if col not in data.columns:
                logger.warning(f"Column {col} missing in data; filling with 0")
                data[col] = 0

        # Aggregate recent performance (last 5 games per player)
        cat_cols: List[str] = [
            c for c in self.config["categorical_columns"] if c != "web_name"
        ]
        num_cols: List[str] = [
            c for c in data.columns if c in self.config["numerical_columns"]
        ]

        players = (
            data.sort_values(["web_name", "gameweek"], ascending=[True, False])
            .groupby("web_name")
            .head(5)
            .groupby("web_name")
            .agg({**{c: "mean" for c in num_cols}, **{c: "first" for c in cat_cols}})
            .reset_index()
        )
        logger.info(f"Processing {len(players)} players")

        # Add upcoming fixture info
        gw_matches = fetch_gw_match_data(
            self.gameweek, self.config["team_name_mapping"]
        )
        normalizer = self.config.get("gw_team_name_mapping", {})

        players["team_name"] = players["team_name"].map(
            lambda x: normalizer.get(str(x), str(x)) if pd.notna(x) else None
        )
        players["gameweek"] = self.gameweek
        players["opponent_team_name"] = players["team_name"].map(
            lambda t: gw_matches.get(str(t), {}).get("opponent_team_name", None)
            if pd.notna(t)
            else None
        )
        players["was_home"] = players["team_name"].map(
            lambda t: gw_matches.get(str(t), {}).get("was_home", None)
            if pd.notna(t)
            else None
        )

        # Predict points
        logger.info("Generating predictions using ensemble models")
        predictions = np.mean([model.predict(players) for model in self.models], axis=0)
        players["expected_points"] = predictions

        return players[self.config["categorical_columns"] + ["expected_points"]]

    def select_optimal_team(self, predictions: pd.DataFrame) -> pd.DataFrame:
        """Select optimal 15-player team."""
        logger.info("Selecting optimal 15-player team")
        team = (
            pd.concat(
                [
                    predictions[predictions.element_type == pos].nlargest(
                        count, "expected_points"
                    )
                    for pos, count in self.TEAM_SELECTION.items()
                ]
            )
            .sort_values("expected_points", ascending=False)
            .reset_index(drop=True)
        )

        # Assign roles
        team["role"] = ""
        team.loc[0, "role"] = "captain"
        team.loc[1, "role"] = "vice"
        team["element_type"] = team["element_type"].map(self.POSITION_MAPPING)

        logger.info(
            f"Captain: {team.loc[0, 'web_name']} ({team.loc[0, 'expected_points']:.2f} pts)"
        )
        logger.info(
            f"Vice-captain: {team.loc[1, 'web_name']} ({team.loc[1, 'expected_points']:.2f} pts)"
        )
        logger.info(f"Total expected points: {team['expected_points'].sum():.2f}")

        return team


if __name__ == "__main__":
    config = load_config("config/config.yaml")
    scout = FPLScout(config)
    predictions = scout.get_player_predictions(
        "data/external/fpl-data-stats-2.csv", gameweek=1
    )
    team = scout.select_optimal_team(predictions)
    print(team)

import joblib
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional

from src.logger import get_logger
from src.utils import fetch_gw_match_data, load_config

import warnings
warnings.filterwarnings("ignore")

# Configure logger
logger = get_logger(__name__)

class FPLScout:
    """Fantasy Premier League player scout for prediction and team selection."""

    def __init__(self, config, data_path: str, gameweek: Optional[int] = None):
        # Configuration constants
        self.config = config
        self.data_path = data_path
        self.CATEGORICAL_COLS = config['categorical_columns']
        self.NUMERICAL_COLS = config['numerical_columns']
        self.TOP_N_BY_POSITION = {1: 2, 2: 5, 3: 5, 4: 3}  # GK, DEF, MID, FWD
        self.POSITION_MAPPING = {
            1: 'Goalkeeper',
            2: 'Defender',
            3: 'Midfielder',
            4: 'Forward',
            5: 'Coach'  # Placeholder for coach
        }
        self.MAX_RECENT_GAMES = max(config.get('max_recent_games', 5), 5)

        logger.info("Initializing FPLScout...")
        # Load models and data
        self.models = self._load_models([meta['path'] for meta in config['models'].values()])
        self.data = self._load_data(data_path, gameweek)
        self.gameweek = gameweek or self._set_gameweek()

        self.team_mapping = config.get('team_name_mapping', {})
        self.team_name_normalizer = config.get('gw_team_name_mapping', {})

        logger.info(f"FPLScout initialized for gameweek {self.gameweek}.")

    def _load_models(self, model_paths: List[str]) -> List[Any]:
        """Load machine learning models from file paths."""
        logger.info(f"Loading models from paths: {model_paths}")
        models = [joblib.load(path) for path in model_paths]
        logger.info(f"Loaded {len(models)} models.")
        return models

    def _ensure_numeric_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure configured numerical columns exist; create missing ones with 0 and coerce to numeric."""
        if df.empty:
            return df

        numeric_cols = list(self.NUMERICAL_COLS or [])

        # Create missing numeric columns with 0
        for col in numeric_cols:
            if col not in df.columns:
                logger.warning(f"Column {col} missing in data. Creating with default 0.")
                df[col] = 0

        return df

    def _load_data(self, data_path: Optional[str], gameweek: Optional[int] = None) -> pd.DataFrame:
        """Load and filter player data."""
        if not data_path:
            logger.warning("No data path provided. Returning empty DataFrame.")
            return pd.DataFrame()

        logger.info(f"Loading data from {data_path}")
        data = pd.read_csv(data_path)
        data = self._ensure_numeric_columns(data)

        if gameweek:
            data = data[data.gameweek <= gameweek].copy()
            logger.info(f"Filtered data for gameweek {gameweek}.")
        logger.info(f"Loaded {len(data)} rows of player data.")
        return data

    def _set_gameweek(self) -> int:
        """Determine the next gameweek based on loaded data."""
        if self.data.empty or 'gameweek' not in self.data.columns:
            return 1
        next_gw = int(self.data['gameweek'].max()) + 1
        if not (1 <= next_gw <= 38):
            next_gw = 1
        return next_gw

    def _preprocess_player_data(self, df: pd.DataFrame, n: int) -> pd.DataFrame:
        """Preprocess player data for prediction."""
        # Get last n rows per player, then aggregate
        # Exclude categorical columns from aggregation
        cat_cols = ['element_type', 'team_name', 'opponent_team_name', 'was_home', 'gameweek']
        num_cols = [col for col in df.drop(["web_name"], axis=1).columns if col not in cat_cols]
        return (
            df.sort_values(['web_name', 'gameweek'], ascending=[True, False])
              .groupby('web_name')
              .head(n)
              .groupby('web_name')
              .agg({col: 'mean' for col in num_cols} | {col: 'first' for col in cat_cols})
        ).reset_index(drop=False)

    def _lazy_predict_player_points(self, player_data: pd.DataFrame) -> np.ndarray:
        """Predict player points using ensemble of models."""
        predictions = [model.predict(player_data) for model in self.models]
        mean_pred = np.mean(predictions, axis=0)
        logger.debug(f"Predicted points for {len(player_data)} players")
        return mean_pred

    def get_player_predictions(self) -> pd.DataFrame:
        """Generate predictions for all players."""
        logger.info("Generating player predictions...")

        gw_match_data = fetch_gw_match_data(self.gameweek, self.config["team_name_mapping"])

        def get_opponent(x):
            match = gw_match_data.get(x)
            return match["opponent_team_name"] if isinstance(match, dict) and "opponent_team_name" in match else None

        def get_was_home(x):
            match = gw_match_data.get(x)
            return match["was_home"] if isinstance(match, dict) and "was_home" in match else None

        def normalize_team_names(x):
            if x in self.team_name_normalizer:
                return self.team_name_normalizer[x]
            return x

        # Preprocess data
        player_predictions = self._preprocess_player_data(self.data, self.MAX_RECENT_GAMES)
        # TODO: post process player_predictions to match new fpl data format
        player_predictions[["team_name","opponent_team_name"]] = player_predictions[["team_name","opponent_team_name"]].applymap(normalize_team_names)
        player_predictions["gameweek"] = self.gameweek
        player_predictions["opponent_team_name"] = player_predictions["team_name"].apply(get_opponent)
        player_predictions["was_home"] = player_predictions["team_name"].apply(get_was_home)

        # Generate predictions
        predictions = self._lazy_predict_player_points(player_predictions)
        player_predictions["expected_points"] = predictions

        # Return only required columns
        result = player_predictions[self.CATEGORICAL_COLS + ['expected_points']].copy()

        logger.info("Player predictions complete.")
        return result

    def select_optimal_team(self, player_predictions: pd.DataFrame) -> List[Dict[str, Any]]:
        """Select optimal team based on predicted points."""
        logger.info("Selecting optimal team based on predictions...")
        selected_players = []

        for position, max_count in self.TOP_N_BY_POSITION.items():
            position_players = player_predictions[
                player_predictions.element_type == position
            ].nlargest(max_count, 'expected_points')
            logger.info(f"Selected top {max_count} for position {self.POSITION_MAPPING[position]}.")
            selected_players.append(position_players)

        team_df = self._finalize_team_selection(selected_players)
        logger.info("Optimal team selection complete.")
        return team_df.to_dict(orient='records')

    def _finalize_team_selection(self, selected_players: List[pd.DataFrame]) -> pd.DataFrame:
        """Finalize team selection with captaincy assignments."""
        team_df = pd.concat(selected_players, ignore_index=True)
        team_df = team_df.sort_values('expected_points', ascending=False)
        team_df = team_df.drop_duplicates(
            subset=['web_name', 'element_type'],
            keep='first'
        ).reset_index(drop=True)

        # Assign captain and vice-captain
        team_df["role"] = ""
        if len(team_df) >= 2:
            team_df.loc[0, "role"] = "captain"
            team_df.loc[1, "role"] = "vice"
            logger.info(f"Assigned captain: {team_df.loc[0, 'web_name']}, vice: {team_df.loc[1, 'web_name']}.")

        # Map position IDs to names
        team_df.element_type = team_df.element_type.map(self.POSITION_MAPPING)

        return team_df

if __name__ == "__main__":
    # Example usage
    config = load_config("config/config.yaml")
    data_path = "data/external/fpl-data-stats-2.csv"
    gameweek = 1

    def main():
        # Initialize scout and generate team selection
        logger.info("Starting FPLScout main routine...")
        scout = FPLScout(config, data_path, gameweek)
        player_predictions = scout.get_player_predictions()
        optimal_team = scout.select_optimal_team(player_predictions)
        optimal_team_df = pd.DataFrame(optimal_team)
        logger.info("Optimal team:")
        print(optimal_team_df)

    main()

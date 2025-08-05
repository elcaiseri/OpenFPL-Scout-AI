from glob import glob
from logging import config
import joblib
import pandas as pd
import numpy as np
import asyncio
from typing import List, Dict, Any, Optional
from logger import get_logger
from utils import load_config

import warnings
warnings.filterwarnings("ignore")

# Configure logger
logger = get_logger(__name__)
config = load_config('config/config.yaml')
print(f"Configuration loaded: {config}")


class FPLScout:
    """Fantasy Premier League player scout for prediction and team selection."""

    def __init__(self, model_paths: List[str], data_path: Optional[str] = None, gameweek: Optional[int] = None):
        # Configuration constants
        self.CATEGORICAL_COLS = config['categorical_columns']
        self.TOP_N_BY_POSITION = {1: 2, 2: 5, 3: 5, 4: 3}  # GK, DEF, MID, FWD
        self.POSITION_MAPPING = {
            1: 'Goalkeeper',
            2: 'Defender',
            3: 'Midfielder',
            4: 'Forward'
        }
        self.MAX_RECENT_GAMES = 5
        self.GAMEWEEK_THRESHOLD = 38

        logger.info("Initializing FPLScout...")
        # Load models and data
        self.models = self._load_models(model_paths)
        self.data = self._load_data(data_path)
        self.gameweek = gameweek or self._get_next_gameweek()
        logger.info(f"FPLScout initialized for gameweek {self.gameweek}.")

    def _load_models(self, model_paths: List[str]) -> List[Any]:
        """Load machine learning models from file paths."""
        logger.info(f"Loading models from paths: {model_paths}")
        models = [joblib.load(path) for path in model_paths]
        logger.info(f"Loaded {len(models)} models.")
        return models

    def _load_data(self, data_path: Optional[str]) -> pd.DataFrame:
        """Load and filter player data."""
        if not data_path:
            logger.warning("No data path provided. Returning empty DataFrame.")
            return pd.DataFrame()

        logger.info(f"Loading data from {data_path}")
        data = pd.read_csv(data_path)
        filtered_data = data[data.gameweek < self.GAMEWEEK_THRESHOLD]
        logger.info(f"Loaded {len(filtered_data)} rows of player data (filtered by gameweek < {self.GAMEWEEK_THRESHOLD}).")
        return filtered_data

    def _get_next_gameweek(self) -> int:
        """Get the next gameweek number (1-38)."""
        if self.data.empty or self.data.gameweek.max() < 1:
            next_gw = 1
        else:
            next_gw = int(self.data.gameweek.max()) + 1
        # Ensure next_gw is within 1 and 38
        next_gw = max(1, min(next_gw, self.GAMEWEEK_THRESHOLD))
        logger.info(f"Next gameweek determined as {next_gw}.")
        return next_gw

    def _preprocess_player_data(self,
                                player_data: pd.DataFrame,
                                opponent_team: str,
                                is_home: bool) -> pd.DataFrame:
        """Preprocess player data for prediction."""
        processed_data = player_data.copy()
        processed_data = processed_data.sort_values('gameweek', ascending=False)

        # Add match context
        processed_data["gameweek"] = self.gameweek
        processed_data["opponent_team_name"] = opponent_team
        processed_data["was_home"] = is_home

        logger.debug(f"Preprocessed data for player {processed_data['web_name'].iloc[0]} (opponent: {opponent_team}, home: {is_home}).")
        return processed_data[:self.MAX_RECENT_GAMES]

    async def _predict_player_points(self, player_data: pd.DataFrame) -> float:
        """Asynchronously predict player points using ensemble of models."""
        loop = asyncio.get_event_loop()
        futures = [
            loop.run_in_executor(None, model.predict, player_data)
            for model in self.models
        ]
        predictions = await asyncio.gather(*futures)
        mean_pred = np.mean(predictions).item()
        logger.debug(f"Predicted points: {mean_pred} for player {player_data['web_name'].iloc[0]}")
        return mean_pred

    async def get_player_predictions(self) -> pd.DataFrame:
        """Generate predictions for all players."""
        logger.info("Generating player predictions...")
        prediction_tasks = []

        for (player_name, team_name), group in self.data.groupby(['web_name', 'team_name']):
            # TODO: Replace with realistic fixture data
            opponent_team = np.random.choice(self.data['team_name'].unique())
            is_home = np.random.choice([True, False])

            processed_group = self._preprocess_player_data(group, opponent_team, is_home)
            prediction_tasks.append(self._create_prediction_task(processed_group))

        results = await asyncio.gather(*prediction_tasks)
        logger.info("Player predictions complete.")
        return pd.concat(results, ignore_index=True)

    async def _create_prediction_task(self, processed_group: pd.DataFrame) -> pd.DataFrame:
        """Create prediction task for a single player."""
        prediction = await self._predict_player_points(processed_group)

        result_columns = self.CATEGORICAL_COLS
        result_data = processed_group[result_columns].drop_duplicates()
        result_data['expected_points'] = prediction

        logger.debug(f"Prediction for {result_data['web_name'].iloc[0]}: {prediction}")
        return result_data

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
    data_path = "data/external/fpl-data-stats-2.csv"
    model_paths = [meta_data['path'] for meta_data in config['models'].values()]

    async def main():
        # Initialize scout and generate team selection
        logger.info("Starting FPLScout main routine...")
        scout = FPLScout(model_paths, data_path)
        player_predictions = await scout.get_player_predictions()
        optimal_team = scout.select_optimal_team(player_predictions)
        optimal_team_df = pd.DataFrame(optimal_team)
        logger.info("Optimal team:")
        print(optimal_team_df)

    asyncio.run(main())

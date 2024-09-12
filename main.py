import os
import argparse
import numpy as np
import pandas as pd
from src.utils import load_config, load_model, plot_top_players
from src.predictor import fplpredictor


class FPLAnalyzer:
    def __init__(self, data_path, config_path='config/config.yaml'):
        """
        Initializes the FPLAnalyzer with data path and configuration settings.

        Args:
            data_path (str): Path to the FPL data file.
            config_path (str): Path to the configuration file.
        """
        self.data_path = data_path
        self.config = load_config(config_path)
        self.models = self.load_models({
            'pipeline': 'models/processor.pkl',
            'linear_regression': 'models/linear_regression.pkl',
            'xgboost': 'models/xgboost_model.pkl'
        })
        self.predictions = pd.DataFrame()

    def load_models(self, model_paths):
        """
        Load the necessary models for predictions.

        Args:
            model_paths (dict): Dictionary containing paths to the models.

        Returns:
            dict: Loaded models including pipeline, linear regression, and XGBoost.
        """
        return {
            'pipeline': load_model(model_paths['pipeline']),
            'linear_regression': load_model(model_paths['linear_regression']),
            'xgboost': load_model(model_paths['xgboost'])
        }

    def run_predictions(self):
        """
        Run predictions using the loaded models and data.

        Returns:
            pd.DataFrame: DataFrame containing the predictions.
        """
        self.predictions, _ = fplpredictor(
            data_path=self.data_path,
            api_url=self.config['api_config']['url'],
            api_key=os.getenv("api_key", ""),
            pipeline=self.models['pipeline'],
            categorical_columns=self.config['categorical_columns'],
            numerical_columns=self.config['numerical_columns'],
            target_column=self.config['target_column'],
            inference_columns=self.config['categorical_columns'] + ["now_cost"] + self.config['target_column'],
            team_name_mapping=self.config['team_name_mapping'],
            lr=self.models['linear_regression'],
            xgboost_model=self.models['xgboost']
        )
        return self.predictions

    def select_best_team(self, budget=100.0, auto_select_bench=False):
        """
        Selects the best FPL team based on predicted points within a given budget.

        Args:
            budget (float): The total budget for selecting the team.
            auto_select_bench (bool): If True, automatically selects the cheapest player for each position as bench players.

        Returns:
            pd.DataFrame: DataFrame containing the selected players for the team.
        """
        if self.predictions.empty:
            print("No predictions available for team selection.")
            return pd.DataFrame()

        self.predictions['points_square_per_million'] = np.log(self.predictions['total_points'] ** 2 / self.predictions['now_cost'])
        self.predictions = self.predictions.sort_values(by='points_square_per_million', ascending=False).reset_index(drop=True)

        position_limits = {'goalkeepers': 2, 'defenders': 5, 'midfielders': 5, 'forwards': 3}
        team_limits = 3

        team = {'goalkeepers': [], 'defenders': [], 'midfielders': [], 'forwards': []}
        team_count = {}
        total_cost = 0.0

        position_map = {1: 'goalkeepers', 2: 'defenders', 3: 'midfielders', 4: 'forwards'}

        def can_add_player(player, position):
            if len(team[position]) >= position_limits[position]:
                return False
            if team_count.get(player['team_name'], 0) >= team_limits:
                return False
            if total_cost + player['now_cost'] > budget:
                return False
            return True

        if auto_select_bench:
            self.predictions['points_square_per_million'] = np.exp(self.predictions['points_square_per_million'])
            for element_type, position in position_map.items():
                cheapest_player = self.predictions[self.predictions['element_type'] == element_type].nsmallest(1, 'now_cost').iloc[0]
                team[position].append(cheapest_player)
                total_cost += cheapest_player['now_cost']
                team_count[cheapest_player['team_name']] = team_count.get(cheapest_player['team_name'], 0) + 1

        for _, player in self.predictions.iterrows():
            position = position_map[player['element_type']]
            if can_add_player(player, position):
                team[position].append(player)
                total_cost += player['now_cost']
                team_count[player['team_name']] = team_count.get(player['team_name'], 0) + 1

        selected_team = pd.concat([pd.DataFrame(team[pos]) for pos in team])
        selected_team.element_type = selected_team.element_type.map(position_map)
        expected_points = selected_team['total_points'].sum()

        print(f"Total Cost: {total_cost:.1f}M, Expected Points: {expected_points:.1f} Points, Bank: {budget-total_cost:.1f}M, Low budget: {auto_select_bench}")
        return selected_team

    def plot_top_players(self, tops=32):
        """
        Save and plot the top players based on predictions.

        Args:
            tops (int): Number of top players to plot.
        """
        if self.predictions.empty:
            print("No predictions available for plotting.")
            return
        gameweek = self.predictions.gameweek.iloc[0]
        save_path = f'data/external/fpl_prediction_gameweek{gameweek}.png'
        plot_top_players(self.predictions, gameweek=gameweek, tops=tops, save_path=save_path)
        print(f"Predictions saved and plotted: {save_path}")

    def run(self, budget, tops, auto_select_bench, run_top_players, run_best_team):
        """
        Run both top player prediction and best team selection based on specified options.

        Args:
            budget (float): The total budget for selecting the team.
            tops (int): Number of top players to plot.
            auto_select_bench (bool): Whether to auto-select the cheapest players as bench.
            run_top_players (bool): Whether to run the top players prediction.
            run_best_team (bool): Whether to run the best team selection.
        """
        self.run_predictions()
        if run_top_players:
            print("Running top players prediction...")
            self.plot_top_players(tops)
        if run_best_team:
            print("Running best team selection...")
            selected_team = self.select_best_team(budget, auto_select_bench)
            print(selected_team)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run FPL prediction and team selection.")
    parser.add_argument('--data_path', type=str, default='data/external/fpl-data-stats2025.csv', help='Path to the data file.')
    parser.add_argument('--budget', type=float, default=100.0, help='Total budget for selecting the team.')
    parser.add_argument('--tops', type=int, default=32, help='Number of top players to plot.')
    parser.add_argument('--auto_select_bench', action='store_true', help='Enable auto-selecting the cheapest bench players.')
    parser.add_argument('--run_top_players', action='store_true', help='Run the top players prediction.')
    parser.add_argument('--run_best_team', action='store_true', help='Run the best team selection.')

    args = parser.parse_args()

    analyzer = FPLAnalyzer(data_path=args.data_path)
    analyzer.run(
        budget=args.budget,
        tops=args.tops,
        auto_select_bench=args.auto_select_bench,
        run_top_players=args.run_top_players,
        run_best_team=args.run_best_team
    )
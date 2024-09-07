# find_best_team.py

import numpy as np
import pandas as pd
import argparse
from src.utils import load_config, load_model
from src.predictor import fplpredictor
from src.find_top_players import load_models, run_predictions


def select_best_team(predictions, budget=100.0, auto_select_bench=False):
    """
    Selects the best FPL team based on predicted points within a given budget.

    Args:
        predictions (pd.DataFrame): DataFrame containing players' predictions including `element_type`, `web_name`,
                                    `team_name`, `now_cost`, and `total_points`.
        budget (float): The total budget for selecting the team.
        auto_select_bench (bool): If True, automatically selects the cheapest player for each position as bench players.

    Returns:
        pd.DataFrame: DataFrame containing the selected players for the team.
    """
    # Sort the predictions by total points per cost to maximize points per million spent
    predictions['points_sqaure_per_million'] = np.log(predictions['total_points'] ** 2 / predictions['now_cost'])
    predictions = predictions.sort_values(by='points_sqaure_per_million', ascending=False).reset_index(drop=True)

    # Position and team limits
    position_limits = {
        'goalkeepers': 2,  # Goalkeepers
        'defenders': 5,    # Defenders
        'midfielders': 5,  # Midfielders
        'forwards': 3      # Forwards
    }
    team_limits = 3  # Max players per team

    # Initialize the team and constraints trackers
    team = {'goalkeepers': [], 'defenders': [], 'midfielders': [], 'forwards': []}
    team_count = {}  # Track the count of players from each team
    total_cost = 0.0

    # Map element_type to the appropriate position keys in the team dictionary
    position_map = {
        1: 'goalkeepers',
        2: 'defenders',
        3: 'midfielders',
        4: 'forwards'
    }

    # Function to check if a player can be added based on position and team constraints
    def can_add_player(player, position):
        # Check position limits
        if len(team[position]) >= position_limits[position]:
            return False
        # Check team limits
        if team_count.get(player['team_name'], 0) >= team_limits:
            return False
        # Check budget limit
        if total_cost + player['now_cost'] > budget:
            return False
        return True

    # Auto-select bench players if enabled
    if auto_select_bench:
        predictions['points_sqaure_per_million'] = np.exp(predictions['points_sqaure_per_million'])

        # Find the cheapest player for each position to form the bench
        for element_type, position in position_map.items():
            cheapest_player = predictions[predictions['element_type'] == element_type].nsmallest(1, 'now_cost').iloc[0]
            team[position].append(cheapest_player)
            total_cost += cheapest_player['now_cost']
            team_count[cheapest_player['team_name']] = team_count.get(cheapest_player['team_name'], 0) + 1

    # Iterate over sorted players to select the best team for the main squad
    for _, player in predictions.iterrows():
        # Get the position string from the position_map
        position = position_map[player['element_type']]

        # Try adding the player to the appropriate position group if constraints allow
        if can_add_player(player, position):
            team[position].append(player)
            total_cost += player['now_cost']
            team_count[player['team_name']] = team_count.get(player['team_name'], 0) + 1

    # Combine all position groups into a single DataFrame
    selected_team = pd.concat([pd.DataFrame(team[pos]) for pos in team])
    selected_team.element_type = selected_team.element_type.map(position_map)
    
    # Calculate the expected total points for the selected team
    expected_points = selected_team['total_points'].sum()

    print(f"Total Cost: {total_cost:.1f}M, Expected Points: {expected_points:.1f} Points, Bank: {budget-total_cost:.1f}M, Low budget: {auto_select_bench}")

    return selected_team


def main(data_path, budget, auto_select_bench):
    """
    Main function to find the best FPL team within a budget.

    Args:
        data_path (str): Path to the data file to be used for predictions.
        budget (float): The total budget for selecting the team.
        auto_select_bench (bool): Whether to automatically select the cheapest player for each position as bench players.
    """
    try:
        # Load configuration
        config = load_config('config/config.yaml')
        
        # Define paths for the models
        model_paths = {
            'pipeline': 'models/processor.pkl',
            'linear_regression': 'models/linear_regression.pkl',
            'xgboost': 'models/xgboost_model.pkl'
        }
        
        # Load the models
        pipeline, lr, xgboost_model = load_models(model_paths)
        if not all([pipeline, lr, xgboost_model]):
            print("Failed to load models. Exiting...")
            return
        
        # Run predictions
        predictions = run_predictions(data_path, config, pipeline, lr, xgboost_model)
        if predictions.empty:
            print("No predictions available for team selection.")
            return
        
        # Select the best team within the given budget
        selected_team = select_best_team(predictions, budget=budget, auto_select_bench=auto_select_bench)
        print(selected_team.sort_values("total_points", ascending=False).head(15))

    except Exception as e:
        print(f"An error occurred: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Select the best FPL team within a given budget.")
    parser.add_argument('--data_path', type=str, default='data/external/fpl-data-stats2025.csv', help='Path to the FPL data file.')
    parser.add_argument('--budget', type=float, default=100.0, help='Total budget for selecting the team.')
    parser.add_argument('--auto_select_bench', action='store_true', help='Enable auto-selecting the cheapest bench players.')

    args = parser.parse_args()
    main(data_path=args.data_path, budget=args.budget, auto_select_bench=args.auto_select_bench)
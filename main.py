# main.py

import argparse
from src.find_top_players import main as find_top_players
from src.find_best_team import main as find_best_team


def main(data_path, budget, tops, auto_select_bench, run_top_players, run_best_team):
    """
    Main function to run both the top player prediction and the best team selection.

    Args:
        data_path (str): Path to the data file to be used for predictions.
        budget (float): The total budget for selecting the team.
        auto_select_bench (bool): Whether to automatically select the cheapest player for each position as bench players.
        run_top_players (bool): Whether to run the top players prediction.
        run_best_team (bool): Whether to run the best team selection.
    """
    if run_top_players:
        # Run the top players prediction
        print("Running top players prediction...")
        find_top_players(data_path=data_path, tops=tops)

    if run_best_team:
        # Run the best team selection
        print("Running best team selection...")
        find_best_team(data_path=data_path, budget=budget, auto_select_bench=auto_select_bench)


if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Run FPL prediction and team selection.")
    parser.add_argument('--data_path', type=str, default='data/external/fpl-data-stats2025.csv', help='Path to the data file.')
    parser.add_argument('--budget', type=float, default=100.0, help='Total budget for selecting the team.')
    parser.add_argument('--tops', type=int, default=32, help='No. of Top player.')
    parser.add_argument('--auto_select_bench', action='store_true', help='Enable auto-selecting the cheapest bench players.')
    parser.add_argument('--run_top_players', action='store_true', help='Run the top players prediction.')
    parser.add_argument('--run_best_team', action='store_true', help='Run the best team selection.')

    # Parse the arguments
    args = parser.parse_args()

    # Call the main function with parsed arguments
    main(
        data_path=args.data_path,
        budget=args.budget,
        tops=args.tops,
        auto_select_bench=args.auto_select_bench,
        run_top_players=args.run_top_players,
        run_best_team=args.run_best_team
    )
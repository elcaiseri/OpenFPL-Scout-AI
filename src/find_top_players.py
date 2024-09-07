# find_top_players.py

import os
import argparse
from src.utils import load_config, load_model, plot_top_players
from src.predictor import fplpredictor

def load_models(model_paths):
    """
    Load necessary models and the pipeline for predictions.

    Args:
        model_paths (dict): Dictionary containing paths to the models.

    Returns:
        tuple: Loaded pipeline, linear regression model, and XGBoost model.
    """
    pipeline = load_model(model_paths['pipeline'])
    lr = load_model(model_paths['linear_regression'])
    xgboost_model = load_model(model_paths['xgboost'])
    return pipeline, lr, xgboost_model

def run_predictions(data_path, config, pipeline, lr, xgboost_model):
    """
    Run the FPL prediction process.

    Args:
        data_path (str): Path to the data file.
        config (dict): Configuration dictionary.
        pipeline (Pipeline): Loaded preprocessing pipeline.
        lr (Model): Loaded Linear Regression model.
        xgboost_model (Model): Loaded XGBoost model.

    Returns:
        pd.DataFrame: DataFrame with predictions.
    """
    predictions, _ = fplpredictor(
        data_path=data_path,
        api_url=config['api_config']['url'],
        api_key=os.getenv("api_key", ""),
        pipeline=pipeline,
        categorical_columns=config['categorical_columns'],
        numerical_columns=config['numerical_columns'],
        target_column=config['target_column'],
        inference_columns=config['categorical_columns'] + ["now_cost"] + config['target_column'],
        team_name_mapping=config['team_name_mapping'],
        lr=lr,
        xgboost_model=xgboost_model
    )
    return predictions

def save_and_plot_predictions(predictions, tops=32):
    """
    Save and plot the predictions.

    Args:
        predictions (pd.DataFrame): DataFrame containing the predictions.
    """
    gameweek = predictions.gameweek.iloc[0]
    save_path = f'data/external/fpl_prediction_gameweek{gameweek}.png'
    plot_top_players(predictions, gameweek=gameweek, tops=tops, save_path=save_path)
    print(f"Predictions saved and plotted: {save_path}")

def main(data_path, tops=32):
    """
    Main function to run FPL predictor to find top players.

    Args:
        data_path (str): Path to the data file.
    """
    try:
        # Load configuration and models
        config = load_config('config/config.yaml')
        model_paths = {
            'pipeline': 'models/processor.pkl',
            'linear_regression': 'models/linear_regression.pkl',
            'xgboost': 'models/xgboost_model.pkl'
        }
        pipeline, lr, xgboost_model = load_models(model_paths)

        # Run predictions
        predictions = run_predictions(data_path, config, pipeline, lr, xgboost_model)

        # Save and plot predictions
        save_and_plot_predictions(predictions, tops)

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find top FPL players for the next gameweek.")
    parser.add_argument('--data_path', type=str, default='data/external/fpl-data-stats2025.csv', help='Path to the data file.')
    
    args = parser.parse_args()
    main(data_path=args.data_path)
# src/inference.py

import pandas as pd
import numpy as np
from src.utils import load_data, fetch_match_data
from src.data_preparation import clean_data, transform_data

def merge_data(left, right, match_data, categorical_columns, target_column, next_gameweek):
    """
    Merge processed data with match information.

    Args:
        left (pd.DataFrame): Processed left DataFrame.
        right (pd.DataFrame): Processed right DataFrame.
        match_data (pd.DataFrame): Match data fetched from the API.
        categorical_columns (list): List of categorical columns.
        target_column (str): Target column name.
        next_gameweek (int): The next gameweek value.

    Returns:
        pd.DataFrame: Merged DataFrame ready for inference.
    """
    left_ = left.merge(match_data, on="team_name", how="left", suffixes=("_left", ""))
    left_["gameweek"] = next_gameweek  # Set the gameweek value to the next gameweek
    X = left_[categorical_columns].merge(right, on="web_name")
    X[target_column] = 0  # Placeholder for target column
    return X

def predictor(data, lr, xgboost_model, target_column):
    """
    Run inference on the data using the loaded models.

    Args:
        data (pd.DataFrame): Transformed input data for inference.
        lr (Model): Loaded Linear Regression model.
        xgboost_model (Model): Loaded XGBoost model.
        target_column (str): The name of the target column.

    Returns:
        np.ndarray: Array of predictions.
    """
    data = data.drop(target_column, axis=1, errors='ignore')
    preds = (1 * lr.predict(data)[:, 0] + 7 * xgboost_model.predict(data)) / 8
    return preds.reshape(-1, 1)

def prepare_next_gameweek_data(data_path, api_url, api_key, pipeline, categorical_columns, numerical_columns, target_column, team_name_mapping):
    """
    Prepare data for the next gameweek by loading, cleaning, merging match data, and transforming it with the pipeline.

    Args:
        data_path (str): Path to the data file.
        api_url (str): API URL for fetching match data.
        api_key (str): API key for accessing the match data.
        pipeline (Pipeline): Preprocessing pipeline for data transformation.
        categorical_columns (list): List of categorical columns.
        numerical_columns (list): List of numerical columns.
        target_column (str): Name of the target column.
        team_name_mapping (dict): Mapping of team names from API to standardized names.

    Returns:
        pd.DataFrame: The processed DataFrame ready for inference for the next gameweek.
    """
    # Load and clean the inference data
    infer_df = load_data(data_path)
    cleaned_data = clean_data(infer_df)
    
    # Determine the next gameweek based on the latest data
    next_gameweek = cleaned_data['gameweek'].max() + 1
    look_back_gameweek = 4 # usually month
    cleaned_data = cleaned_data[cleaned_data['gameweek'] < next_gameweek].copy() if next_gameweek > 1 else cleaned_data
    
    # Prepare left and right DataFrames for merging
    left = cleaned_data[categorical_columns].drop_duplicates(subset=["web_name"]).sort_values("web_name").reset_index(drop=True)
    #right = cleaned_data[["web_name"] + numerical_columns].groupby("web_name").mean().round(6).reset_index()
    right = cleaned_data.sort_values(by=['web_name', 'gameweek']).groupby('web_name').tail(look_back_gameweek)[["web_name"] + numerical_columns].groupby("web_name", as_index=False).mean()#.round(6)

    # Fetch match data from the API for the next gameweek
    next_match_data = fetch_match_data(api_url, api_key, next_gameweek, team_name_mapping)

    # Merge data with match information
    X = merge_data(left, right, next_match_data, categorical_columns, target_column, next_gameweek)
    
    # Transform data using the loaded pipeline
    next_gameweek_data = transform_data(pipeline, X)
    
    return X, next_gameweek_data

def fplpredictor(data_path, api_url, api_key, pipeline, categorical_columns, numerical_columns, target_column, inference_columns, team_name_mapping, lr, xgboost_model):
    """
    Perform prediction for the next gameweek using the specified models.

    Args:
        data_path (str): Path to the data file.
        api_url (str): API URL for fetching match data.
        api_key (str): API key for accessing the match data.
        pipeline (Pipeline): Preprocessing pipeline for data transformation.
        categorical_columns (list): List of categorical columns.
        numerical_columns (list): List of numerical columns.
        target_column (str): Name of the target column.
        inference_columns (list): List of columns to display after inference.
        team_name_mapping (dict): Mapping of team names from API to standardized names.
        lr (Model): Loaded Linear Regression model.
        xgboost_model (Model): Loaded XGBoost model.

    Returns:
        pd.DataFrame: DataFrame with predictions sorted by the target column.
    """
    # Prepare next gameweek data
    X, next_gameweek_data = prepare_next_gameweek_data(
        data_path=data_path,
        api_url=api_url,
        api_key=api_key,
        pipeline=pipeline,
        categorical_columns=categorical_columns,
        numerical_columns=numerical_columns,
        target_column=target_column,
        team_name_mapping=team_name_mapping
    )
    
    # Run inference and add predictions to DataFrame
    X[target_column] = predictor(next_gameweek_data, lr, xgboost_model, target_column)
    
    # Select and display final predictions
    gameweek_predictions = X[inference_columns].sort_values(target_column, ascending=False)
    
    return gameweek_predictions, next_gameweek_data
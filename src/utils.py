import pandas as pd
import requests
import yaml
import joblib
import seaborn as sns
import matplotlib.pyplot as plt

def load_data(file_path):
    """
    Load and preprocess the inference dataset.
    
    Args:
        file_path (str): Path to the CSV file containing data.
    
    Returns:
        pd.DataFrame: Preprocessed DataFrame.
    """
    try:
        df = pd.read_csv(file_path)
        print(f"Data loaded successfully from {file_path} with shape {df.shape}.")
        return df
    except FileNotFoundError:
        print(f"File not found: {file_path}")
    except pd.errors.EmptyDataError:
        print(f"No data: {file_path} is empty.")
    except Exception as e:
        print(f"Error loading data from {file_path}: {e}")
    return pd.DataFrame()

def load_config(config_path):
    """
    Load configuration settings from a YAML file.
    
    Args:
        config_path (str): Path to the configuration file.
    
    Returns:
        dict: Configuration settings.
    """
    try:
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)
        print(f"Configuration loaded successfully from {config_path}.")
        return config
    except FileNotFoundError:
        print(f"File not found: {config_path}")
    except yaml.YAMLError:
        print(f"Error parsing the YAML configuration file: {config_path}")
    except Exception as e:
        print(f"Error loading configuration from {config_path}: {e}")
    return {}

def fetch_match_data(api_url, api_key, gameweek, team_name_mapping):
    """
    Fetch match data from an external football API.
    
    Args:
        api_url (str): API endpoint URL.
        api_key (str): API key for authentication.
        gameweek (int): The gameweek number to fetch.
        team_name_mapping (dict): Mapping of team names from API to standardized names.
    
    Returns:
        pd.DataFrame: DataFrame containing match data.
    """
    headers = {"X-Auth-Token": api_key}
    try:
        response = requests.get(f"{api_url}?matchday={gameweek}", headers=headers)
        response.raise_for_status()  # Raises an HTTPError for bad responses (4xx, 5xx)
        data = response.json()

        matches_data = [
            {'team_name': team_name_mapping.get(match['homeTeam']['name'], match['homeTeam']['name']), 
             'opponent_team_name': team_name_mapping.get(match['awayTeam']['name'], match['awayTeam']['name']), 
             'was_home': True}
            for match in data['matches']
        ] + [
            {'team_name': team_name_mapping.get(match['awayTeam']['name'], match['awayTeam']['name']), 
             'opponent_team_name': team_name_mapping.get(match['homeTeam']['name'], match['homeTeam']['name']), 
             'was_home': False}
            for match in data['matches']
        ]

        print(f"Match data fetched successfully for gameweek {gameweek}.")
        return pd.DataFrame(matches_data)
    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred: {http_err}")
    except requests.exceptions.RequestException as req_err:
        print(f"Request error: {req_err}")
    except Exception as e:
        print(f"Error fetching match data: {e}")
    return pd.DataFrame()

def load_model(model_path):
    """
    Load a model or preprocessing pipeline from a specified path.
    
    Args:
        model_path (str): The path to the model or pipeline file.
    
    Returns:
        object: The loaded model or pipeline.
    """
    try:
        model = joblib.load(model_path)
        print(f"Model loaded successfully from {model_path}.")
        return model
    except FileNotFoundError:
        print(f"File not found: {model_path}")
    except joblib.externals.loky.process_executor.TerminatedWorkerError:
        print(f"Error loading model due to a terminated worker: {model_path}")
    except Exception as e:
        print(f"Error loading model from {model_path}: {e}")
    return None

def plot_top_players(data, gameweek, tops, save_path=None):
    """
    Plot the top players based on total points and save the plot if save_path is provided.

    Args:
        data (pd.DataFrame): DataFrame containing player data.
        gameweek (int): The gameweek number for the title.
        tops (int): Number of top players to display.
        save_path (str, optional): Path to save the figure. Defaults to None.

    Returns:
        None
    """
    # Filter and sort data by total points for better visualization
    data_sorted = data.head(tops)

    # Set style for Seaborn
    sns.set(style="whitegrid")

    # Plotting
    plt.figure(figsize=(12, 8))  # Adjusted figure size for a vertical layout
    sns.barplot(
        x='total_points', 
        y='web_name', 
        data=data_sorted, 
        palette='dark',
        orient='h'
    )

    # Adjust font size for readability
    plt.xticks(fontsize=10)
    plt.yticks(fontsize=10)
    plt.xlabel('Total Points', fontsize=10)
    plt.ylabel('Player Name', fontsize=10)
    plt.title(f'FPL Prediction - Top {tops} - Total Points of Players (Gameweek {gameweek})', fontsize=14)
    plt.grid(axis='x', linestyle='--', alpha=0.5)

    # Add text in the bottom right with the GitHub link
    plt.text(
        0.8, 0.2, 
        "X.com/@elcaiseri", 
        fontsize=16, 
        color='red', 
        ha='right', 
        va='bottom', 
        transform=plt.gcf().transFigure
    )

    plt.tight_layout()
    
    # Save the figure if save_path is provided
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        print(f"Figure saved as {save_path}")
    
    plt.show()
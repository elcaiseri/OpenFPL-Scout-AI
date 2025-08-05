import pandas as pd
import yaml

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



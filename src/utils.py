import yaml


def load_config(config_path):
    """Load configuration settings from a YAML file."""
    try:
        with open(config_path, "r") as file:
            config = yaml.safe_load(file)
        return config
    except (FileNotFoundError, yaml.YAMLError, Exception):
        return {}

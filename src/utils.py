import yaml


def load_config(config_path):
    """Load configuration settings from a YAML file.

    A missing or malformed file raises with its real cause instead of
    returning an empty config that fails later with a misleading error.
    """
    with open(config_path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping")
    return config

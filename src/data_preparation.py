from src.utils import load_config

# Load configuration from config.yaml
config = load_config('config/config.yaml')

# Extract relevant settings from the configuration
drop_cols = config['drop_cols']
categorical_columns = config['categorical_columns']
numerical_columns = config['numerical_columns']
target_column = config['target_column']

def clean_data(df):
    """
    Perform data cleaning by dropping unnecessary columns.

    Args:
        df (pd.DataFrame): The DataFrame to clean.

    Returns:
        pd.DataFrame: The cleaned DataFrame with specified columns removed.
    """
    try:
        # Drop columns specified in the config
        df = df.drop(drop_cols, axis=1)
        print(f"Data cleaned successfully. Dropped columns: {drop_cols}")
        return df
    except KeyError as e:
        print(f"Error cleaning data: {e}")
        return df

def transform_data(pipeline, data):
    """
    Transform data using the specified preprocessing pipeline.

    Args:
        pipeline (Pipeline): The preprocessing pipeline for transforming data.
        data (pd.DataFrame): The DataFrame to transform.

    Returns:
        pd.DataFrame: The transformed DataFrame with updated column names.
    """
    try:
        # Transform data using the provided pipeline
        data_ = pipeline.transform(data[categorical_columns + numerical_columns + target_column])
        # Clean column names by removing prefixes
        data_.columns = [col.split('__')[-1] for col in data_.columns]
        print("Data transformed successfully using the pipeline.")
        return data_
    except Exception as e:
        print(f"Error transforming data: {e}")
        return data
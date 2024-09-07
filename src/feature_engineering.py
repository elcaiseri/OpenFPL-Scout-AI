def feature_engineering(df):
    """
    Perform feature engineering on the DataFrame by adding lag, rolling averages, and interaction features.

    Args:
        df (pd.DataFrame): The input DataFrame containing the necessary columns for feature engineering.

    Returns:
        pd.DataFrame: The DataFrame with additional engineered features.
    """
    try:
        # 1. Create Lag Features: Previous game week points and rolling averages
        # Use shift to create lag features for total points
        df['total_points_lag_1'] = df.groupby('web_name')['total_points'].shift(1)

        # Use rolling with groupby, ensuring index alignment by resetting index correctly
        rolling_3 = df.groupby('ordinal-name__web_name')['total_points'].rolling(3, min_periods=1).mean().reset_index(level=0, drop=True)
        rolling_5 = df.groupby('ordinal-name__web_name')['total_points'].rolling(5, min_periods=1).mean().reset_index(level=0, drop=True)

        # Ensure alignment with the main DataFrame's index
        df['total_points_rolling_3'] = rolling_3.values
        df['total_points_rolling_5'] = rolling_5.values

        # Fill NaN values from lag and rolling features
        df.fillna(0, inplace=True)

        # 2. Interaction Features: Create interaction terms
        df['minutes_xP'] = df['minutes'] * df['xP']

        print("Feature engineering completed successfully.")
        return df

    except KeyError as e:
        print(f"Error during feature engineering: Missing column {e}")
        return df
    except Exception as e:
        print(f"Unexpected error during feature engineering: {e}")
        return df
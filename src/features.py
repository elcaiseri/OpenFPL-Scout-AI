"""Shared feature preparation for model training and live predictions."""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


# The upstream data changed to descriptive column names in 2025-26.  Keep one
# canonical schema so a model trained on older seasons also works on new data.
COLUMN_ALIASES = {
    "shots": "total_shots",
    "SoT": "shots_on_target",
    "SiB": "shots_in_box",
    "xG": "expected_goals",
    "npxG": "non_penalty_expected_goals",
    "G": "goals",
    "npG": "non_penalty_goals",
    "key_passes": "chances_created",
    "xA": "expected_assists",
    "A": "assists",
    "xGC": "expected_goals_conceded",
    "GC": "goals_conceded",
    "xCS": "expected_clean_sheet",
    "CS": "clean_sheet",
    "xGI": "expected_goal_involvements",
    "npxGI": "non_penalty_expected_goal_involvements",
    "xP": "expected_points",
    "Att Pen": "touches_opp_box",
    "penalty_area_touches": "touches_opp_box",
}

TEAM_NAME_ALIASES = {
    "Man Utd": "Manchester United",
    "Spurs": "Tottenham",
    "Newcastle": "Newcastle United",
    "Wolves": "Wolverhampton Wanderers",
    "Nott'm Forest": "Nottingham Forest",
    "Man City": "Manchester City",
}

CATEGORICAL_FEATURES = [
    "element_type",
    "web_name",
    "team_name",
    "opponent_team_name",
    "was_home",
]

# These values are averaged over the five matches before the match being
# predicted. Missing season-specific statistics remain NaN and are imputed in
# each CV fold, preventing information from the validation season leaking in.
HISTORY_FEATURES = [
    "now_cost",
    "selected_by_percent",
    "minutes",
    "total_shots",
    "shots_on_target",
    "shots_in_box",
    "expected_goals",
    "non_penalty_expected_goals",
    "goals",
    "non_penalty_goals",
    "chances_created",
    "expected_assists",
    "assists",
    "expected_goals_conceded",
    "goals_conceded",
    "expected_clean_sheet",
    "clean_sheet",
    "clearances",
    "shot_blocks",
    "interceptions",
    "recoveries",
    "tackles",
    "clearances_blocks_interceptions",
    "defensive_contribution",
    "expected_goal_involvements",
    "non_penalty_expected_goal_involvements",
    "expected_points",
    "PvsxP",
    "touches",
    "touches_opp_box",
    "carries_final_third",
    "carries_penalty_area",
]

NUMERICAL_FEATURES = ["gameweek", *HISTORY_FEATURES]
MODEL_FEATURES = [*CATEGORICAL_FEATURES, *NUMERICAL_FEATURES]


def normalize_fpl_columns(data: pd.DataFrame) -> pd.DataFrame:
    """Return FPL data using the current canonical names and team labels."""
    normalized = data.copy()
    for old_name, canonical_name in COLUMN_ALIASES.items():
        if old_name not in normalized.columns:
            continue
        if canonical_name in normalized.columns:
            normalized[canonical_name] = normalized[canonical_name].combine_first(
                normalized[old_name]
            )
            normalized = normalized.drop(columns=old_name)
        else:
            normalized = normalized.rename(columns={old_name: canonical_name})

    for column in ("team_name", "opponent_team_name"):
        if column in normalized.columns:
            normalized[column] = normalized[column].replace(TEAM_NAME_ALIASES)
    return normalized


def ensure_feature_columns(
    data: pd.DataFrame, feature_columns: Iterable[str] = MODEL_FEATURES
) -> pd.DataFrame:
    """Add absent model inputs as NaN and return them in training order."""
    prepared = data.copy()
    for column in feature_columns:
        if column not in prepared.columns:
            prepared[column] = np.nan
    return prepared[list(feature_columns)]


def add_rolling_history(
    data: pd.DataFrame, window: int = 5, shift: int = 1
) -> pd.DataFrame:
    """Replace match statistics with each player's prior-match rolling means."""
    player_key = "id" if "id" in data.columns else "web_name"
    prepared = data.sort_values(["_season", player_key, "gameweek"]).copy()
    for column in HISTORY_FEATURES:
        if column not in prepared.columns:
            prepared[column] = np.nan

    group_keys = [prepared["_season"], prepared[player_key]]
    history = prepared[HISTORY_FEATURES].apply(pd.to_numeric, errors="coerce")
    prepared[HISTORY_FEATURES] = history.astype(float)
    shifted = history.groupby(group_keys, sort=False).shift(shift)
    rolling = shifted.groupby(group_keys, sort=False).rolling(
        window, min_periods=1
    ).mean()
    rolling.index = rolling.index.droplevel([0, 1])

    # A double gameweek has two target rows but both forecasts are made before
    # the gameweek begins. Give both fixtures the history available at the
    # start of that gameweek instead of leaking fixture one into fixture two.
    keys = prepared[["_season", player_key, "gameweek"]]
    first_positions = np.flatnonzero(keys.ne(keys.shift()).any(axis=1).to_numpy())
    group_lengths = np.diff(np.append(first_positions, len(prepared)))
    rolling_values = np.repeat(
        rolling.iloc[first_positions].to_numpy(), group_lengths, axis=0
    )
    prepared[HISTORY_FEATURES] = rolling_values
    return prepared

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

ROLLING_WINDOWS = (3, 5, 10)
TEMPORAL_FEATURES = [
    "points_last",
    "points_mean_3",
    "points_mean_5",
    "points_mean_10",
    "points_std_5",
    "points_trend_3_10",
    "minutes_last",
    "minutes_mean_3",
    "minutes_mean_10",
    "minutes_std_5",
    "appearance_probability_5",
    "start_probability_5",
    "expected_goal_involvements_mean_3",
    "expected_goal_involvements_mean_10",
    "expected_points_last",
]
CONTEXT_FEATURES = ["fixture_difficulty", "fixture_count"]

NUMERICAL_FEATURES = [
    "gameweek",
    *HISTORY_FEATURES,
    *TEMPORAL_FEATURES,
    *CONTEXT_FEATURES,
]
MODEL_FEATURES = [*CATEGORICAL_FEATURES, *NUMERICAL_FEATURES]
INFERENCE_REQUIRED_COLUMNS = [
    "id",
    "element_type",
    "web_name",
    "team_name",
    "gameweek",
]


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


def prepare_recent_player_features(
    data: pd.DataFrame, gameweek: int, history_window: int = 5
) -> pd.DataFrame:
    """Build one inference row per player from matches before ``gameweek``."""
    if history_window < 1:
        raise ValueError("history_window must be at least 1")

    prepared = normalize_fpl_columns(data)
    missing = sorted(set(INFERENCE_REQUIRED_COLUMNS).difference(prepared.columns))
    if missing:
        raise ValueError(f"Inference data is missing required columns: {missing}")
    if prepared.empty:
        raise ValueError("Inference data is empty")

    prepared["gameweek"] = pd.to_numeric(prepared["gameweek"], errors="coerce")
    prepared = prepared.loc[prepared["gameweek"] < gameweek].copy()
    if prepared.empty:
        raise ValueError(f"No player history is available before gameweek {gameweek}")

    numeric_history = [*HISTORY_FEATURES, "total_points"]
    for column in numeric_history:
        if column not in prepared.columns:
            prepared[column] = np.nan
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce")

    prepared = prepared.sort_values(
        ["id", "gameweek"], ascending=[True, False], kind="stable"
    )
    max_window = max(history_window, *ROLLING_WINDOWS)
    recent = prepared.groupby("id", sort=False, group_keys=False).head(max_window)
    latest = recent.drop_duplicates("id", keep="first").set_index("id")
    base_recent = recent.groupby("id", sort=False, group_keys=False).head(
        history_window
    )
    history = base_recent.groupby("id", sort=False)[HISTORY_FEATURES].mean()

    def recent_stat(column: str, window: int, statistic: str = "mean") -> pd.Series:
        values = recent.groupby("id", sort=False, group_keys=False).head(window)
        grouped = values.groupby("id", sort=False)[column]
        if statistic == "std":
            return grouped.std(ddof=0)
        return grouped.mean()

    temporal = pd.DataFrame(index=history.index)
    temporal["points_last"] = latest["total_points"]
    temporal["minutes_last"] = latest["minutes"]
    temporal["expected_points_last"] = latest["expected_points"]
    for window in ROLLING_WINDOWS:
        temporal[f"points_mean_{window}"] = recent_stat("total_points", window)
    temporal["points_std_5"] = recent_stat("total_points", 5, "std")
    temporal["points_trend_3_10"] = (
        temporal["points_mean_3"] - temporal["points_mean_10"]
    )
    temporal["minutes_mean_3"] = recent_stat("minutes", 3)
    temporal["minutes_mean_10"] = recent_stat("minutes", 10)
    temporal["minutes_std_5"] = recent_stat("minutes", 5, "std")

    availability = recent[["id", "minutes"]].copy()
    availability["appearance"] = (availability["minutes"] > 0).where(
        availability["minutes"].notna()
    )
    availability["start"] = (availability["minutes"] >= 60).where(
        availability["minutes"].notna()
    )
    recent_five_availability = availability.groupby(
        "id", sort=False, group_keys=False
    ).head(5)
    temporal["appearance_probability_5"] = recent_five_availability.groupby(
        "id", sort=False
    )["appearance"].mean()
    temporal["start_probability_5"] = recent_five_availability.groupby(
        "id", sort=False
    )["start"].mean()
    temporal["expected_goal_involvements_mean_3"] = recent_stat(
        "expected_goal_involvements", 3
    )
    temporal["expected_goal_involvements_mean_10"] = recent_stat(
        "expected_goal_involvements", 10
    )

    identity_columns = [
        "web_name",
        "element_type",
        "team_name",
        "opponent_team_name",
        "was_home",
    ]
    for column in identity_columns:
        if column not in latest.columns:
            latest[column] = np.nan

    players = (
        latest[identity_columns]
        .join(history, how="left")
        .join(temporal, how="left")
        .reset_index()
    )
    players["gameweek"] = int(gameweek)
    players["fixture_difficulty"] = np.nan
    players["fixture_count"] = np.nan
    return players


def estimate_fixture_difficulty(
    data: pd.DataFrame, gameweek: int, window: int = 5
) -> dict[str, float]:
    """Estimate 1–5 opponent difficulty from prior team FPL-point form."""
    prepared = normalize_fpl_columns(data)
    required = {"team_name", "gameweek", "total_points"}
    if required.difference(prepared.columns):
        return {}

    prepared["gameweek"] = pd.to_numeric(prepared["gameweek"], errors="coerce")
    prepared["total_points"] = pd.to_numeric(
        prepared["total_points"], errors="coerce"
    )
    prepared = prepared.loc[prepared["gameweek"] < gameweek]
    team_weeks = (
        prepared.groupby(["team_name", "gameweek"], as_index=False)["total_points"]
        .sum(min_count=1)
        .sort_values(["team_name", "gameweek"])
    )
    recent = team_weeks.groupby("team_name", sort=False, group_keys=False).tail(
        window
    )
    strength = recent.groupby("team_name", sort=False)["total_points"].mean()
    if strength.empty:
        return {}
    difficulty = 1.0 + 4.0 * strength.rank(method="average", pct=True)
    return {str(team): float(value) for team, value in difficulty.items()}


def add_rolling_history(
    data: pd.DataFrame, window: int = 5, shift: int = 1
) -> pd.DataFrame:
    """Build leakage-safe prior-match means, trends, availability, and context."""
    player_key = "id" if "id" in data.columns else "web_name"
    prepared = data.sort_values(
        ["_season", player_key, "gameweek"], kind="stable"
    ).copy()
    for column in ("team_name", "opponent_team_name"):
        if column not in prepared.columns:
            prepared[column] = np.nan
    numeric_history = [*HISTORY_FEATURES, "total_points"]
    for column in numeric_history:
        if column not in prepared.columns:
            prepared[column] = np.nan

    group_keys = [prepared["_season"], prepared[player_key]]
    history = prepared[numeric_history].apply(pd.to_numeric, errors="coerce")
    prepared[HISTORY_FEATURES] = history[HISTORY_FEATURES].astype(float)
    prepared["total_points"] = history["total_points"].astype(float)
    shifted = history.groupby(group_keys, sort=False).shift(shift)

    def rolling_frame(values: pd.DataFrame, size: int, statistic: str) -> pd.DataFrame:
        rolling = values.groupby(group_keys, sort=False).rolling(
            size, min_periods=1
        )
        result = rolling.std(ddof=0) if statistic == "std" else rolling.mean()
        result.index = result.index.droplevel([0, 1])
        return result

    # A double gameweek has two target rows but both forecasts are made before
    # the gameweek begins. Give both fixtures the history available at the
    # start of that gameweek instead of leaking fixture one into fixture two.
    keys = prepared[["_season", player_key, "gameweek"]]
    first_positions = np.flatnonzero(keys.ne(keys.shift()).any(axis=1).to_numpy())
    group_lengths = np.diff(np.append(first_positions, len(prepared)))

    def freeze_gameweek(values: pd.DataFrame) -> np.ndarray:
        return np.repeat(
            values.iloc[first_positions].to_numpy(), group_lengths, axis=0
        )

    base_rolling = rolling_frame(shifted[HISTORY_FEATURES], window, "mean")
    prepared[HISTORY_FEATURES] = freeze_gameweek(base_rolling)

    temporal = pd.DataFrame(index=prepared.index)
    temporal["points_last"] = shifted["total_points"]
    temporal["minutes_last"] = shifted["minutes"]
    temporal["expected_points_last"] = shifted["expected_points"]
    point_windows = {}
    for rolling_window in ROLLING_WINDOWS:
        point_windows[rolling_window] = rolling_frame(
            shifted[["total_points"]], rolling_window, "mean"
        )["total_points"]
        temporal[f"points_mean_{rolling_window}"] = point_windows[rolling_window]
    temporal["points_std_5"] = rolling_frame(
        shifted[["total_points"]], 5, "std"
    )["total_points"]
    temporal["points_trend_3_10"] = point_windows[3] - point_windows[10]
    temporal["minutes_mean_3"] = rolling_frame(
        shifted[["minutes"]], 3, "mean"
    )["minutes"]
    temporal["minutes_mean_10"] = rolling_frame(
        shifted[["minutes"]], 10, "mean"
    )["minutes"]
    temporal["minutes_std_5"] = rolling_frame(
        shifted[["minutes"]], 5, "std"
    )["minutes"]

    availability = pd.DataFrame(index=prepared.index)
    availability["appearance"] = (history["minutes"] > 0).where(
        history["minutes"].notna()
    )
    availability["start"] = (history["minutes"] >= 60).where(
        history["minutes"].notna()
    )
    shifted_availability = availability.groupby(group_keys, sort=False).shift(shift)
    temporal["appearance_probability_5"] = rolling_frame(
        shifted_availability[["appearance"]], 5, "mean"
    )["appearance"]
    temporal["start_probability_5"] = rolling_frame(
        shifted_availability[["start"]], 5, "mean"
    )["start"]
    temporal["expected_goal_involvements_mean_3"] = rolling_frame(
        shifted[["expected_goal_involvements"]], 3, "mean"
    )["expected_goal_involvements"]
    temporal["expected_goal_involvements_mean_10"] = rolling_frame(
        shifted[["expected_goal_involvements"]], 10, "mean"
    )["expected_goal_involvements"]
    prepared[TEMPORAL_FEATURES] = freeze_gameweek(temporal[TEMPORAL_FEATURES])

    prepared["fixture_count"] = prepared.groupby(
        ["_season", player_key, "gameweek"], sort=False
    )["gameweek"].transform("size").astype(float)

    team_weeks = (
        prepared.groupby(
            ["_season", "team_name", "gameweek"], as_index=False
        )["total_points"]
        .sum(min_count=1)
        .sort_values(["_season", "team_name", "gameweek"])
    )
    team_weeks["prior_form"] = team_weeks.groupby(
        ["_season", "team_name"], sort=False
    )["total_points"].transform(
        lambda values: values.shift(shift).rolling(5, min_periods=1).mean()
    )
    team_weeks["derived_difficulty"] = team_weeks.groupby(
        ["_season", "gameweek"], sort=False
    )["prior_form"].rank(method="average", pct=True)
    team_weeks["derived_difficulty"] = 1.0 + 4.0 * team_weeks[
        "derived_difficulty"
    ]
    difficulty_lookup = {
        (int(season), int(gameweek), str(team)): difficulty
        for season, gameweek, team, difficulty in zip(
            team_weeks["_season"],
            team_weeks["gameweek"],
            team_weeks["team_name"],
            team_weeks["derived_difficulty"],
        )
    }
    derived_difficulty = pd.Series(
        [
            difficulty_lookup.get(
                (int(season), int(gameweek), str(opponent)), np.nan
            )
            for season, gameweek, opponent in zip(
                prepared["_season"],
                prepared["gameweek"],
                prepared["opponent_team_name"],
            )
        ],
        index=prepared.index,
        dtype=float,
    )
    if "fixture_difficulty" in prepared.columns:
        supplied = pd.to_numeric(prepared["fixture_difficulty"], errors="coerce")
        prepared["fixture_difficulty"] = supplied.combine_first(derived_difficulty)
    else:
        prepared["fixture_difficulty"] = derived_difficulty
    return prepared

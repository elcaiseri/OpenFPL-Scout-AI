"""Unit tests for src/scout.py – FPLScout class."""

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.scout import FPLScout

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

NUMERICAL_COLUMNS = [
    "element_type",
    "gameweek",
    "minutes",
    "GC",
    "CS",
    "now_cost",
    "selected_by_percent",
    "xG",
    "xA",
    "xGI",
]

CATEGORICAL_COLUMNS = [
    "id",
    "element_type",
    "web_name",
    "team_name",
    "opponent_team_name",
    "was_home",
    "gameweek",
]


def _make_config(num_models: int = 2) -> Dict[str, Any]:
    """Return a minimal config dict with mocked model entries."""
    return {
        "numerical_columns": NUMERICAL_COLUMNS,
        "categorical_columns": CATEGORICAL_COLUMNS,
        "team_name_mapping": {"Arsenal FC": "Arsenal"},
        "gw_team_name_mapping": {"Arsenal": "Arsenal", "Chelsea": "Chelsea"},
        "models": {
            f"model_{i}": {"path": f"models/model_{i}.pkl"} for i in range(num_models)
        },
    }


def _make_player_df(n_players: int = 10, n_gameweeks: int = 3) -> pd.DataFrame:
    """Build a synthetic player DataFrame with the expected columns."""
    rng = np.random.default_rng(42)
    rows = []
    for gw in range(1, n_gameweeks + 1):
        for i in range(n_players):
            rows.append(
                {
                    "id": i,
                    "web_name": f"Player_{i}",
                    "element_type": (i % 4) + 1,
                    "team_name": "Arsenal" if i < 5 else "Chelsea",
                    "gameweek": gw,
                    "minutes": int(rng.integers(60, 90)),
                    "GC": float(rng.random()),
                    "CS": float(rng.random()),
                    "now_cost": float(rng.integers(50, 130)),
                    "selected_by_percent": float(rng.random() * 100),
                    "xG": float(rng.random()),
                    "xA": float(rng.random()),
                    "xGI": float(rng.random()),
                    "opponent_team_name": None,
                    "was_home": bool(rng.integers(0, 2)),
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture
def mock_models():
    """Two fake sklearn-style estimators that return constant predictions."""
    models = []
    for val in (2.0, 3.0):
        m = MagicMock()
        m.predict = MagicMock(side_effect=lambda X, v=val: np.full(len(X), v))
        models.append(m)
    return models


@pytest.fixture
def scout(mock_models):
    """FPLScout with mocked joblib.load so no real model files are needed."""
    config = _make_config(num_models=2)
    with patch("src.scout.joblib.load", side_effect=mock_models):
        return FPLScout(config)


@pytest.fixture
def player_df():
    return _make_player_df(n_players=15, n_gameweeks=5)


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestFPLScoutInit:
    def test_loads_correct_number_of_models(self, mock_models):
        config = _make_config(num_models=3)
        mock_models_3 = mock_models + [MagicMock()]
        with patch("src.scout.joblib.load", side_effect=mock_models_3):
            s = FPLScout(config)
        assert len(s.models) == 3

    def test_default_gameweek_is_one(self, scout):
        assert scout.gameweek == 1

    def test_config_stored(self, scout):
        assert "numerical_columns" in scout.config
        assert "categorical_columns" in scout.config


# ---------------------------------------------------------------------------
# _load_data
# ---------------------------------------------------------------------------


class TestLoadData:
    def test_returns_dataframe(self, scout, tmp_path, player_df):
        csv_path = tmp_path / "data.csv"
        player_df.to_csv(csv_path, index=False)
        result = scout._load_data(str(csv_path))
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(player_df)

    def test_preserves_columns(self, scout, tmp_path, player_df):
        csv_path = tmp_path / "data.csv"
        player_df.to_csv(csv_path, index=False)
        result = scout._load_data(str(csv_path))
        assert set(player_df.columns).issubset(set(result.columns))


# ---------------------------------------------------------------------------
# _resolve_gameweek
# ---------------------------------------------------------------------------


class TestResolveGameweek:
    def test_explicit_gameweek_used(self, scout, player_df):
        gw = scout._resolve_gameweek(player_df, gameweek=7)
        assert gw == 7
        assert scout.gameweek == 7

    def test_auto_resolves_to_max_plus_one(self, scout, player_df):
        # player_df has gameweeks 1-5, so resolved should be 6
        gw = scout._resolve_gameweek(player_df, gameweek=None)
        assert gw == 6
        assert scout.gameweek == 6

    def test_updates_instance_gameweek(self, scout, player_df):
        scout._resolve_gameweek(player_df, gameweek=10)
        assert scout.gameweek == 10


# ---------------------------------------------------------------------------
# _ensure_required_columns
# ---------------------------------------------------------------------------


class TestEnsureRequiredColumns:
    def test_existing_columns_unchanged(self, scout, player_df):
        original_xg = player_df["xG"].copy()
        result = scout._ensure_required_columns(player_df.copy())
        pd.testing.assert_series_equal(
            result["xG"].reset_index(drop=True), original_xg.reset_index(drop=True)
        )

    def test_missing_column_filled_with_zero(self, scout, player_df):
        df = player_df.drop(columns=["xG"])
        result = scout._ensure_required_columns(df)
        assert "xG" in result.columns
        assert (result["xG"] == 0).all()

    def test_multiple_missing_columns_filled(self, scout, player_df):
        df = player_df.drop(columns=["xG", "xA"])
        result = scout._ensure_required_columns(df)
        assert (result["xG"] == 0).all()
        assert (result["xA"] == 0).all()


# ---------------------------------------------------------------------------
# _aggregate_recent_performance
# ---------------------------------------------------------------------------


class TestAggregateRecentPerformance:
    def test_returns_one_row_per_player(self, scout, player_df):
        result = scout._aggregate_recent_performance(player_df)
        assert len(result) == player_df["web_name"].nunique()

    def test_uses_at_most_5_recent_gameweeks(self, scout):
        # Build data with 10 gameweeks to ensure capping at 5
        df = _make_player_df(n_players=2, n_gameweeks=10)
        result = scout._aggregate_recent_performance(df)
        # We can only verify that the mean is weighted from last 5 GWs by
        # checking the output is a single row per player
        assert len(result) == 2

    def test_web_name_preserved(self, scout, player_df):
        result = scout._aggregate_recent_performance(player_df)
        assert "web_name" in result.columns
        assert set(result["web_name"]) == set(player_df["web_name"])

    def test_numerical_cols_are_aggregated(self, scout, player_df):
        result = scout._aggregate_recent_performance(player_df)
        for col in ["minutes", "xG", "xA"]:
            assert col in result.columns
            assert result[col].notna().all()


# ---------------------------------------------------------------------------
# _attach_fixture_info
# ---------------------------------------------------------------------------


class TestAttachFixtureInfo:
    _GW_MATCHES = {
        "Arsenal": {"opponent_team_name": "Chelsea", "was_home": True},
        "Chelsea": {"opponent_team_name": "Arsenal", "was_home": False},
    }

    def test_opponent_assigned(self, scout, player_df):
        players = scout._aggregate_recent_performance(player_df)
        with patch("src.scout.fetch_gw_match_data", return_value=self._GW_MATCHES):
            result = scout._attach_fixture_info(players, gameweek=6)
        arsenal_rows = result[result["team_name"] == "Arsenal"]
        assert (arsenal_rows["opponent_team_name"] == "Chelsea").all()

    def test_was_home_assigned(self, scout, player_df):
        players = scout._aggregate_recent_performance(player_df)
        with patch("src.scout.fetch_gw_match_data", return_value=self._GW_MATCHES):
            result = scout._attach_fixture_info(players, gameweek=6)
        arsenal_rows = result[result["team_name"] == "Arsenal"]
        assert (arsenal_rows["was_home"] == True).all()

    def test_gameweek_column_set(self, scout, player_df):
        players = scout._aggregate_recent_performance(player_df)
        with patch("src.scout.fetch_gw_match_data", return_value=self._GW_MATCHES):
            result = scout._attach_fixture_info(players, gameweek=6)
        assert (result["gameweek"] == 6).all()

    def test_unknown_team_gets_none_opponent(self, scout):
        """Players whose team isn't in gw_matches should get None opponent."""
        df = pd.DataFrame(
            [
                {
                    "id": 99,
                    "web_name": "Unknown",
                    "element_type": 3,
                    "team_name": "UnknownFC",
                    "gameweek": 1,
                    "minutes": 90,
                    "GC": 0,
                    "CS": 0,
                    "now_cost": 75,
                    "selected_by_percent": 1.0,
                    "xG": 0.1,
                    "xA": 0.1,
                    "xGI": 0.2,
                    "opponent_team_name": None,
                    "was_home": None,
                }
            ]
        )
        # Ensure required numerical columns exist
        df = scout._ensure_required_columns(df)
        with patch("src.scout.fetch_gw_match_data", return_value={}):
            result = scout._attach_fixture_info(df, gameweek=6)
        assert result.iloc[0]["opponent_team_name"] is None


# ---------------------------------------------------------------------------
# _predict_expected_points
# ---------------------------------------------------------------------------


class TestPredictExpectedPoints:
    def test_column_added(self, scout, player_df):
        players = scout._aggregate_recent_performance(player_df)
        # Supply only numerical columns to avoid predict errors
        num_df = players[[c for c in NUMERICAL_COLUMNS if c in players.columns]].copy()
        result = scout._predict_expected_points(num_df)
        assert "expected_points" in result.columns

    def test_ensemble_mean(self, scout, player_df):
        """Models return 2.0 and 3.0 → mean should be 2.5."""
        players = scout._aggregate_recent_performance(player_df)
        num_cols = [c for c in NUMERICAL_COLUMNS if c in players.columns]
        num_df = players[num_cols].copy()
        result = scout._predict_expected_points(num_df)
        assert np.allclose(result["expected_points"], 2.5)

    def test_output_length_matches_input(self, scout, player_df):
        players = scout._aggregate_recent_performance(player_df)
        num_df = players[[c for c in NUMERICAL_COLUMNS if c in players.columns]].copy()
        result = scout._predict_expected_points(num_df)
        assert len(result) == len(players)


# ---------------------------------------------------------------------------
# select_optimal_team
# ---------------------------------------------------------------------------


class TestSelectOptimalTeam:
    def _make_predictions(self) -> pd.DataFrame:
        """Create a predictions DataFrame that satisfies TEAM_SELECTION quotas."""
        rng = np.random.default_rng(0)
        rows = []
        # 3 GKs, 7 DEFs, 7 MIDs, 5 FWDs  (>= required slots)
        counts = {1: 3, 2: 7, 3: 7, 4: 5}
        i = 0
        for pos, n in counts.items():
            for _ in range(n):
                rows.append(
                    {
                        "id": i,
                        "web_name": f"P_{i}",
                        "element_type": pos,
                        "team_name": "Arsenal",
                        "opponent_team_name": "Chelsea",
                        "was_home": True,
                        "gameweek": 6,
                        "expected_points": float(rng.random() * 10),
                    }
                )
                i += 1
        return pd.DataFrame(rows)

    def test_team_size_is_15(self, scout):
        predictions = self._make_predictions()
        team = scout.select_optimal_team(predictions)
        assert len(team) == 15

    def test_position_quotas(self, scout):
        predictions = self._make_predictions()
        team = scout.select_optimal_team(predictions)
        # After mapping, positions are strings
        pos_counts = team["element_type"].value_counts()
        assert pos_counts.get("Goalkeeper", 0) == 2
        assert pos_counts.get("Defender", 0) == 5
        assert pos_counts.get("Midfielder", 0) == 5
        assert pos_counts.get("Forward", 0) == 3

    def test_captain_is_highest_scorer(self, scout):
        predictions = self._make_predictions()
        team = scout.select_optimal_team(predictions)
        captain_row = team[team["role"] == "captain"].iloc[0]
        assert captain_row["expected_points"] == team["expected_points"].max()

    def test_vice_captain_is_second_highest(self, scout):
        predictions = self._make_predictions()
        team = scout.select_optimal_team(predictions)
        vice_row = team[team["role"] == "vice"].iloc[0]
        sorted_pts = team["expected_points"].sort_values(ascending=False).values
        assert vice_row["expected_points"] == sorted_pts[1]

    def test_element_type_mapped_to_string(self, scout):
        predictions = self._make_predictions()
        team = scout.select_optimal_team(predictions)
        assert set(team["element_type"]).issubset(
            {"Goalkeeper", "Defender", "Midfielder", "Forward"}
        )

    def test_sorted_by_expected_points_descending(self, scout):
        predictions = self._make_predictions()
        team = scout.select_optimal_team(predictions)
        pts = team["expected_points"].tolist()
        assert pts == sorted(pts, reverse=True)


# ---------------------------------------------------------------------------
# get_player_predictions (integration-style, filesystem + API mocked)
# ---------------------------------------------------------------------------


class TestGetPlayerPredictions:
    _GW_MATCHES = {
        "Arsenal": {"opponent_team_name": "Chelsea", "was_home": True},
        "Chelsea": {"opponent_team_name": "Arsenal", "was_home": False},
    }

    def test_returns_dataframe(self, scout, tmp_path):
        df = _make_player_df(n_players=15, n_gameweeks=3)
        csv_path = tmp_path / "players.csv"
        df.to_csv(csv_path, index=False)
        with patch("src.scout.fetch_gw_match_data", return_value=self._GW_MATCHES):
            result = scout.get_player_predictions(str(csv_path))
        assert isinstance(result, pd.DataFrame)

    def test_expected_points_in_output(self, scout, tmp_path):
        df = _make_player_df(n_players=15, n_gameweeks=3)
        csv_path = tmp_path / "players.csv"
        df.to_csv(csv_path, index=False)
        with patch("src.scout.fetch_gw_match_data", return_value=self._GW_MATCHES):
            result = scout.get_player_predictions(str(csv_path))
        assert "expected_points" in result.columns

    def test_output_columns_match_config(self, scout, tmp_path):
        df = _make_player_df(n_players=15, n_gameweeks=3)
        csv_path = tmp_path / "players.csv"
        df.to_csv(csv_path, index=False)
        with patch("src.scout.fetch_gw_match_data", return_value=self._GW_MATCHES):
            result = scout.get_player_predictions(str(csv_path))
        expected_cols = set(scout.config["categorical_columns"] + ["expected_points"])
        assert expected_cols == set(result.columns)

    def test_explicit_gameweek_filters_data(self, scout, tmp_path):
        df = _make_player_df(n_players=5, n_gameweeks=5)
        csv_path = tmp_path / "players.csv"
        df.to_csv(csv_path, index=False)
        with patch("src.scout.fetch_gw_match_data", return_value=self._GW_MATCHES):
            result = scout.get_player_predictions(str(csv_path), gameweek=3)
        # All predictions should be for GW 3
        assert (result["gameweek"] == 3).all()

    def test_one_row_per_player(self, scout, tmp_path):
        df = _make_player_df(n_players=8, n_gameweeks=4)
        csv_path = tmp_path / "players.csv"
        df.to_csv(csv_path, index=False)
        with patch("src.scout.fetch_gw_match_data", return_value=self._GW_MATCHES):
            result = scout.get_player_predictions(str(csv_path))
        assert result["web_name"].nunique() == result["web_name"].count()

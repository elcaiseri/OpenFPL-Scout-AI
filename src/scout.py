"""Inference and team selection for the FPL scout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import joblib
import numpy as np
import pandas as pd

from src.features import (
    MODEL_FEATURES,
    ensure_feature_columns,
    normalize_fpl_columns,
    prepare_recent_player_features,
)
from src.logger import get_logger

logger = get_logger(__name__)


class InferenceError(RuntimeError):
    """Raised when the configured model ensemble cannot produce predictions."""


@dataclass(frozen=True)
class ModelArtifact:
    """A named, weighted deployment model."""

    name: str
    path: Path
    estimator: Any
    weight: float = 1.0


class FPLScout:
    """Prepare player history, run the model ensemble, and select a squad."""

    POSITION_MAPPING: Dict[int, str] = {
        1: "Goalkeeper",
        2: "Defender",
        3: "Midfielder",
        4: "Forward",
    }
    TEAM_SELECTION: Dict[int, int] = {1: 2, 2: 5, 3: 5, 4: 3}

    def __init__(
        self,
        config: Mapping[str, Any],
        fixture_provider: Optional[
            Callable[..., Mapping[Any, Mapping[str, Any]]]
        ] = None,
        model_loader: Callable[[str], Any] = joblib.load,
    ) -> None:
        self.config = dict(config)
        if fixture_provider is None:
            from src.utils import fetch_gw_match_data

            fixture_provider = fetch_gw_match_data
        self.fixture_provider = fixture_provider
        self._fixture_cache: Dict[int, Mapping[Any, Mapping[str, Any]]] = {}
        self._fixture_cache_lock = RLock()

        inference_config = self.config.get("inference", {})
        self.history_window = int(inference_config.get("history_window", 5))
        self.cache_fixtures = bool(inference_config.get("cache_fixtures", True))
        self.clip_min = inference_config.get("clip_min", 0.0)
        self.clip_max = inference_config.get("clip_max")

        if self.history_window < 1:
            raise ValueError("inference.history_window must be at least 1")

        logger.info("Initializing FPLScout...")
        self.model_artifacts = self._load_models(model_loader)
        # Retain the old list attribute for callers that inspect loaded models.
        self.models: List[Any] = [item.estimator for item in self.model_artifacts]
        requested_minimum = inference_config.get(
            "minimum_successful_models", len(self.model_artifacts)
        )
        self.minimum_successful_models = int(requested_minimum)
        if not 1 <= self.minimum_successful_models <= len(self.model_artifacts):
            raise ValueError(
                "inference.minimum_successful_models must be between 1 and the "
                "number of configured models"
            )

        # Backward-compatible only; new API code reads the gameweek from the
        # returned DataFrame attrs to avoid cross-request state races.
        self.gameweek: Optional[int] = None
        logger.info(
            "Loaded %d models; at least %d must succeed per inference",
            len(self.model_artifacts),
            self.minimum_successful_models,
        )

    def _load_models(
        self, model_loader: Callable[[str], Any]
    ) -> List[ModelArtifact]:
        model_config = self.config.get("models")
        if not isinstance(model_config, Mapping) or not model_config:
            raise ValueError("At least one model must be configured")

        artifacts: List[ModelArtifact] = []
        for name, metadata in model_config.items():
            if not isinstance(metadata, Mapping) or not metadata.get("path"):
                raise ValueError(f"Model {name!r} is missing a path")
            path = Path(str(metadata["path"]))
            try:
                estimator = model_loader(str(path))
            except Exception as error:
                raise InferenceError(
                    f"Failed to load model {name!r} from {path}"
                ) from error
            if not callable(getattr(estimator, "predict", None)):
                raise TypeError(f"Loaded model {name!r} does not provide predict()")

            expected_features = getattr(estimator, "feature_names_in_", None)
            if (
                expected_features is not None
                and list(expected_features) != MODEL_FEATURES
            ):
                raise InferenceError(
                    f"Model {name!r} uses a different feature contract; retrain it "
                    "with the current trainer"
                )

            weight = float(metadata.get("weight", 1.0))
            if not np.isfinite(weight) or weight <= 0:
                raise ValueError(f"Model {name!r} weight must be a positive number")
            artifacts.append(ModelArtifact(name, path, estimator, weight))
        return artifacts

    @staticmethod
    def _resolve_gameweek(data: pd.DataFrame, gameweek: Optional[int]) -> int:
        if gameweek is not None:
            resolved = int(gameweek)
        else:
            if "gameweek" not in data.columns or data.empty:
                raise ValueError("Cannot infer a gameweek from empty or invalid data")
            numeric_gameweeks = pd.to_numeric(data["gameweek"], errors="coerce")
            if numeric_gameweeks.notna().sum() == 0:
                raise ValueError("The gameweek column has no numeric values")
            resolved = int(numeric_gameweeks.max()) + 1
        if resolved < 1:
            raise ValueError("gameweek must be at least 1")
        return resolved

    def _get_fixtures(self, gameweek: int) -> Mapping[Any, Mapping[str, Any]]:
        if self.cache_fixtures:
            with self._fixture_cache_lock:
                cached = self._fixture_cache.get(gameweek)
            if cached is not None:
                return cached

        fixtures = self.fixture_provider(
            gameweek, self.config.get("team_name_mapping", {})
        )
        if not isinstance(fixtures, Mapping):
            raise InferenceError("Fixture provider returned an invalid response")
        if self.cache_fixtures:
            with self._fixture_cache_lock:
                self._fixture_cache[gameweek] = fixtures
        return fixtures

    def _attach_fixture_context(
        self, players: pd.DataFrame, gameweek: int
    ) -> pd.DataFrame:
        result = players.copy()
        normalizer = self.config.get("gw_team_name_mapping", {})
        result["team_name"] = result["team_name"].map(
            lambda team: normalizer.get(str(team), str(team))
            if pd.notna(team)
            else np.nan
        )

        fixtures = self._get_fixtures(gameweek)
        fixture_rows = result["team_name"].map(
            lambda team: fixtures.get(str(team), {}) if pd.notna(team) else {}
        )
        result["gameweek"] = gameweek
        result["opponent_team_name"] = fixture_rows.map(
            lambda fixture: fixture.get("opponent_team_name", np.nan)
        )
        result["was_home"] = fixture_rows.map(
            lambda fixture: fixture.get("was_home", np.nan)
        )
        result = normalize_fpl_columns(result)

        missing_count = int(
            result[["opponent_team_name", "was_home"]].isna().any(axis=1).sum()
        )
        if missing_count:
            missing_teams = sorted(
                str(team)
                for team in result.loc[
                    result["opponent_team_name"].isna(), "team_name"
                ].dropna().unique()
            )
            logger.warning(
                "Missing fixture context for %d players across teams: %s",
                missing_count,
                ", ".join(missing_teams) or "unknown",
            )
        return result

    def _predict_ensemble(
        self, features: pd.DataFrame
    ) -> tuple[np.ndarray, Dict[str, Any]]:
        predictions: List[np.ndarray] = []
        weights: List[float] = []
        successful_models: List[str] = []
        failures: Dict[str, str] = {}

        for artifact in self.model_artifacts:
            try:
                values = np.asarray(artifact.estimator.predict(features), dtype=float)
                values = values.reshape(-1)
                if len(values) != len(features):
                    raise ValueError(
                        f"returned {len(values)} predictions for "
                        f"{len(features)} players"
                    )
                if not np.isfinite(values).all():
                    raise ValueError("returned non-finite predictions")
            except Exception as error:
                failures[artifact.name] = str(error)
                logger.exception("Inference failed for model %s", artifact.name)
                continue

            predictions.append(values)
            weights.append(artifact.weight)
            successful_models.append(artifact.name)

        if len(predictions) < self.minimum_successful_models:
            raise InferenceError(
                f"Only {len(predictions)} of {len(self.model_artifacts)} models "
                f"succeeded; required {self.minimum_successful_models}. "
                f"Failures: {failures}"
            )

        prediction_matrix = np.vstack(predictions)
        ensemble = np.average(prediction_matrix, axis=0, weights=np.asarray(weights))
        if self.clip_min is not None or self.clip_max is not None:
            lower = -np.inf if self.clip_min is None else float(self.clip_min)
            upper = np.inf if self.clip_max is None else float(self.clip_max)
            ensemble = np.clip(ensemble, lower, upper)

        diagnostics = {
            "successful_models": successful_models,
            "failed_models": failures,
            "weights": dict(zip(successful_models, weights)),
            "mean_model_spread": float(prediction_matrix.std(axis=0).mean()),
        }
        return ensemble, diagnostics

    def predict_players(
        self, data: pd.DataFrame, gameweek: Optional[int] = None
    ) -> pd.DataFrame:
        """Generate predictions from an already loaded FPL history frame."""
        normalized = normalize_fpl_columns(data)
        resolved_gameweek = self._resolve_gameweek(normalized, gameweek)
        players = prepare_recent_player_features(
            normalized,
            gameweek=resolved_gameweek,
            history_window=self.history_window,
        )
        players = self._attach_fixture_context(players, resolved_gameweek)
        model_input = ensure_feature_columns(players, MODEL_FEATURES)

        logger.info(
            "Generating gameweek %d predictions for %d players",
            resolved_gameweek,
            len(players),
        )
        ensemble, diagnostics = self._predict_ensemble(model_input)
        players["expected_points"] = ensemble

        output_columns: Sequence[str] = [
            *self.config.get(
                "categorical_columns",
                [
                    "id",
                    "element_type",
                    "web_name",
                    "team_name",
                    "opponent_team_name",
                    "was_home",
                    "gameweek",
                ],
            ),
            "expected_points",
        ]
        for column in output_columns:
            if column not in players.columns:
                players[column] = np.nan
        result = players[list(output_columns)].copy()
        result.attrs["gameweek"] = resolved_gameweek
        result.attrs["inference"] = diagnostics

        self.gameweek = resolved_gameweek
        logger.info(
            "Inference complete with models: %s",
            ", ".join(diagnostics["successful_models"]),
        )
        return result

    def get_player_predictions(
        self, data_path: str, gameweek: Optional[int] = None
    ) -> pd.DataFrame:
        """Load a CSV and generate predictions for all current players."""
        logger.info("Loading inference data from %s", data_path)
        data = pd.read_csv(data_path)
        logger.info("Loaded %d records", len(data))
        return self.predict_players(data, gameweek=gameweek)

    def select_optimal_team(self, predictions: pd.DataFrame) -> pd.DataFrame:
        """Select the highest-ranked 15-player positional squad."""
        required_columns = {"element_type", "web_name", "expected_points"}
        missing = sorted(required_columns.difference(predictions.columns))
        if missing:
            raise ValueError(f"Prediction data is missing required columns: {missing}")
        if predictions.empty:
            raise ValueError("Cannot select a team from empty predictions")

        player_key = "id" if "id" in predictions.columns else "web_name"
        unique_predictions = predictions.drop_duplicates(player_key, keep="first")
        selected: List[pd.DataFrame] = []
        for position, count in self.TEAM_SELECTION.items():
            candidates = unique_predictions.loc[
                unique_predictions["element_type"] == position
            ]
            if len(candidates) < count:
                raise ValueError(
                    f"Need {count} position-{position} players, found {len(candidates)}"
                )
            selected.append(candidates.nlargest(count, "expected_points"))

        team = (
            pd.concat(selected, ignore_index=True)
            .sort_values("expected_points", ascending=False)
            .reset_index(drop=True)
        )
        team["role"] = ""
        team.loc[0, "role"] = "captain"
        team.loc[1, "role"] = "vice"
        team["element_type"] = team["element_type"].map(self.POSITION_MAPPING)

        logger.info(
            "Selected captain %s (%.2f) and vice-captain %s (%.2f); total %.2f",
            team.loc[0, "web_name"],
            team.loc[0, "expected_points"],
            team.loc[1, "web_name"],
            team.loc[1, "expected_points"],
            team["expected_points"].sum(),
        )
        return team


if __name__ == "__main__":
    from src.utils import load_config

    configuration = load_config("config/config.yaml")
    scout = FPLScout(configuration)
    player_predictions = scout.get_player_predictions(
        "data/external/fpl-data-stats-2026.csv", gameweek=1
    )
    print(scout.select_optimal_team(player_predictions))

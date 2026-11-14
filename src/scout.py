"""Inference and team selection for the FPL scout."""

from __future__ import annotations

import copy
import os
import re
import time
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import joblib
import numpy as np
import pandas as pd

from src.features import (
    HISTORY_FEATURES,
    MODEL_FEATURES,
    TEAM_NAME_ALIASES,
    ensure_feature_columns,
    normalize_fpl_columns,
    prepare_recent_player_features,
)
from src.data_archive import DataArchive
from src.fpl_data_inference import FPLDataHistoryProvider
from src.logger import get_logger
from src.official_fpl import OfficialFPLClient
from src.team_rating import _best_benchmark_lineup

logger = get_logger(__name__)


def _environment_bool(name: str) -> Optional[bool]:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _mounted_data_path(configured_path: Any) -> Any:
    """Relocate configured data/* paths when the runtime mount differs."""
    data_root = os.environ.get("OPENFPL_DATA_ROOT")
    if not data_root or configured_path is None:
        return configured_path
    relative_path = Path(str(configured_path))
    if relative_path.is_absolute():
        return relative_path
    parts = relative_path.parts
    if parts and parts[0] == "data":
        parts = parts[1:]
    return Path(data_root).joinpath(*parts)


class InferenceError(RuntimeError):
    """Raised when the configured model ensemble cannot produce predictions."""


# Official availability codes: available, doubtful, injured, suspended,
# unavailable (left the league), and not in the squad.
STATUS_AVAILABILITY = {"a": 1.0, "d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}
AVAILABILITY_COLUMNS = ("status", "chance_of_playing_next_round", "can_select")


def availability_factor(players: pd.DataFrame) -> pd.Series:
    """Return each player's official probability of being available (0–1)."""
    status = players.get("status", pd.Series("a", index=players.index, dtype=object))
    status = status.fillna("a").astype(str).str.casefold()
    availability = status.map(STATUS_AVAILABILITY).fillna(1.0)
    if "chance_of_playing_next_round" in players.columns:
        chance = pd.to_numeric(players["chance_of_playing_next_round"], errors="coerce")
        availability = availability.where(
            chance.isna(), chance.clip(lower=0, upper=100) / 100.0
        )
    if "can_select" in players.columns:
        selectable = players["can_select"].map(
            lambda value: True if pd.isna(value) else bool(value)
        )
        availability = availability.where(selectable, 0.0)
    return availability.astype(float)


def _canonical_team(name: Any) -> Any:
    if pd.isna(name):
        return np.nan
    return TEAM_NAME_ALIASES.get(str(name), str(name))


def _venues(fixtures: List[Mapping[str, Any]]) -> List[Optional[bool]]:
    return [
        None if pd.isna(fixture.get("was_home")) else bool(fixture["was_home"])
        for fixture in fixtures
    ]


def _shared_venue(fixtures: List[Mapping[str, Any]]) -> Any:
    """Return the venue every fixture shares, or NaN when they differ."""
    venues = set(_venues(fixtures))
    if len(venues) == 1 and None not in venues:
        return venues.pop()
    return np.nan


def _opponent_label(fixtures: List[Mapping[str, Any]]) -> Any:
    """Join a gameweek's opponents, marking each venue when they differ."""
    if not fixtures:
        return np.nan
    names = [
        str(_canonical_team(fixture.get("opponent_team_name"))) for fixture in fixtures
    ]
    venues = _venues(fixtures)
    if len(set(venues)) > 1:
        names = [
            name if venue is None else f"{name} ({'H' if venue else 'A'})"
            for name, venue in zip(names, venues)
        ]
    return " / ".join(names)


@dataclass(frozen=True)
class ModelArtifact:
    """A named, weighted deployment model."""

    name: str
    path: Path
    estimator: Any
    weight: float = 1.0


@dataclass(frozen=True)
class _LiveForecast:
    """An open gameweek's live predictions and how long they may be served.

    Until ``fresh_until`` they are served as they are. Until ``stale_until``
    they may still be served to a caller that will request a refresh (see
    ``FPLScout.refresh_predictions``). Both are ``FPLScout.clock`` times.
    """

    predictions: pd.DataFrame
    fresh_until: float
    stale_until: float


def _copy_predictions(predictions: pd.DataFrame) -> pd.DataFrame:
    """A copy a request may change without touching the cached forecast."""
    copied = predictions.copy()
    copied.attrs = copy.deepcopy(predictions.attrs)
    return copied


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
        official_client: Optional[OfficialFPLClient] = None,
        fpl_data_provider: Optional[FPLDataHistoryProvider] = None,
        data_archive: Optional[DataArchive] = None,
    ) -> None:
        self.config = dict(config)
        official_config = self.config.get("official_fpl", {})
        self.official_client = official_client or OfficialFPLClient(
            base_url=official_config.get(
                "base_url", "https://fantasy.premierleague.com/api"
            ),
            timeout=float(official_config.get("timeout_seconds", 20)),
            cache_ttl=int(official_config.get("cache_ttl_seconds", 300)),
            history_cache_ttl=int(
                official_config.get("history_cache_ttl_seconds", 900)
            ),
            max_workers=int(official_config.get("max_workers", 8)),
        )
        if fixture_provider is None:
            fixture_provider = self.official_client.fixtures_for_gameweek
        self.fixture_provider = fixture_provider
        self._fixture_cache: Dict[
            int, Tuple[float, Mapping[Any, Mapping[str, Any]]]
        ] = {}
        self._fixture_cache_lock = RLock()

        inference_config = self.config.get("inference", {})
        self.history_window = int(inference_config.get("history_window", 5))
        self.cache_fixtures = bool(inference_config.get("cache_fixtures", True))
        # Postponements move fixtures between gameweeks, so cached fixture
        # context must expire rather than live for the whole process.
        self.fixture_cache_ttl = float(
            inference_config.get("fixture_cache_ttl_seconds", 300)
        )
        self.clock: Callable[[], float] = time.monotonic
        # Live predictions of an open gameweek only change when official data
        # does, and the official client caches that for minutes, so repeat
        # requests reuse one prediction instead of rerunning the ensemble.
        # A forecast older than the TTL may still be served, up to the stale
        # limit, to a caller that then asks for a refresh in its own request
        # (see refresh_predictions). Nothing runs outside a request: under
        # Cloud Run's request-based billing it would have no CPU. 0 disables.
        self.prediction_cache_ttl = float(
            inference_config.get(
                "prediction_cache_ttl_seconds",
                official_config.get("cache_ttl_seconds", 300),
            )
        )
        self.prediction_stale_ttl = float(
            inference_config.get(
                "prediction_stale_seconds", 3 * self.prediction_cache_ttl
            )
        )
        # After a cold start, the open gameweek's last archived forecast may be
        # served the same way, if it is at most this old.
        self.prediction_restore_max_age = float(
            inference_config.get("prediction_restore_max_age_seconds", 3600)
        )
        if self.prediction_cache_ttl < 0 or self.prediction_restore_max_age < 0:
            raise ValueError("inference prediction cache ages cannot be negative")
        if self.prediction_stale_ttl < self.prediction_cache_ttl:
            raise ValueError(
                "inference.prediction_stale_seconds must be at least "
                "inference.prediction_cache_ttl_seconds"
            )
        self._live_forecasts: Dict[int, _LiveForecast] = {}
        # A prediction under way, which other requests for its gameweek wait
        # for. It always runs in the thread of the request that started it.
        self._pending_forecasts: Dict[int, Future] = {}
        # The official data each cached forecast's availability was last
        # checked against, so the check reruns only when that data changes.
        self._availability_checked: Dict[int, Tuple[_LiveForecast, Any]] = {}
        self._forecast_lock = RLock()
        self.clip_min = inference_config.get("clip_min", 0.0)
        self.clip_max = inference_config.get("clip_max")
        cold_start_config = inference_config.get("cold_start", {})
        self.cold_start_enabled = bool(cold_start_config.get("enabled", True))
        self.max_players_per_team = int(
            inference_config.get("max_players_per_team", 3)
        )
        if self.max_players_per_team < 1:
            raise ValueError("inference.max_players_per_team must be at least 1")

        fpl_data_config = self.config.get("fpl_data_inference", {})
        configured_fpl_data_enabled = bool(fpl_data_config.get("enabled", False))
        environment_override = _environment_bool("FPL_DATA_INFERENCE_ENABLED")
        self.fpl_data_enabled = (
            configured_fpl_data_enabled
            if environment_override is None
            else environment_override
        )
        self.fpl_data_season = str(fpl_data_config.get("season", "")).strip()
        self.fpl_data_permission_status = str(
            fpl_data_config.get("permission_status", "pending")
        )
        acknowledgement_override = _environment_bool(
            "FPL_DATA_ACKNOWLEDGE_PERMISSION_PENDING"
        )
        self.fpl_data_permission_acknowledged = (
            bool(fpl_data_config.get("acknowledge_permission_pending", False))
            if acknowledgement_override is None
            else acknowledgement_override
        )
        # An explicit FPL_DATA_ACKNOWLEDGE_PERMISSION_PENDING=false keeps the
        # service on imported files, even once permission is granted.
        self.fpl_data_local_only = acknowledgement_override is False
        self.fpl_data_start_gameweek = int(fpl_data_config.get("start_gameweek", 2))
        if self.fpl_data_start_gameweek < 2:
            raise ValueError("fpl_data_inference.start_gameweek must be at least 2")
        self.fpl_data_provider = fpl_data_provider
        self.data_archive = data_archive or DataArchive.from_config(self.config)
        self.last_data_enrichment: Dict[str, Any] = {
            "provider": "fpl-data",
            "status": "not-attempted",
        }
        if self.fpl_data_enabled and self.fpl_data_provider is None:
            self.fpl_data_provider = FPLDataHistoryProvider(
                season_value=self.fpl_data_season,
                local_path=_mounted_data_path(fpl_data_config.get("local_path")),
                runtime_cache_path=_mounted_data_path(
                    fpl_data_config.get("runtime_cache_path")
                ),
                refresh_ttl_seconds=int(
                    fpl_data_config.get("refresh_ttl_seconds", 21600)
                ),
                minimum_match_ratio=float(
                    fpl_data_config.get("minimum_match_ratio", 0.8)
                ),
                max_gameweek_lag=int(fpl_data_config.get("max_gameweek_lag", 1)),
                timeout_seconds=float(fpl_data_config.get("timeout_seconds", 60)),
                permission_status=self.fpl_data_permission_status,
                acknowledge_permission_pending=self.fpl_data_permission_acknowledged,
                local_only=self.fpl_data_local_only,
            )

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
        if self.fpl_data_enabled:
            logger.warning(
                "FPL Data inference enrichment is enabled from GW%d with permission "
                "status %s; remote downloads %s",
                self.fpl_data_start_gameweek,
                self.fpl_data_permission_status,
                "allowed"
                if getattr(self.fpl_data_provider, "remote_download_allowed", False)
                else "disabled (local imports only)",
            )

    def _load_models(self, model_loader: Callable[[str], Any]) -> List[ModelArtifact]:
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
            if cached is not None and cached[0] > self.clock():
                return cached[1]

        fixtures = self.fixture_provider(
            gameweek, self.config.get("team_name_mapping", {})
        )
        if not isinstance(fixtures, Mapping):
            raise InferenceError("Fixture provider returned an invalid response")
        if self.cache_fixtures:
            with self._fixture_cache_lock:
                self._fixture_cache[gameweek] = (
                    self.clock() + self.fixture_cache_ttl,
                    fixtures,
                )
        return fixtures

    @staticmethod
    def _team_fixtures(context: Any) -> List[Mapping[str, Any]]:
        """Return every fixture for one team, including both halves of a DGW."""
        if not isinstance(context, Mapping) or not context:
            return []
        fixtures = context.get("fixtures")
        if isinstance(fixtures, (list, tuple)):
            return [fixture for fixture in fixtures if isinstance(fixture, Mapping)]
        return [context]

    def _attach_fixture_context(
        self, players: pd.DataFrame, gameweek: int
    ) -> pd.DataFrame:
        """Attach display context plus the ``_fixtures`` list for each player.

        Blank-gameweek players receive ``fixture_count`` 0 and no opponent;
        double-gameweek players keep every fixture so each can be predicted.
        """
        result = players.copy()
        normalizer = self.config.get("gw_team_name_mapping", {})
        result["team_name"] = result["team_name"].map(
            lambda team: normalizer.get(str(team), str(team))
            if pd.notna(team)
            else np.nan
        )

        fixtures = self._get_fixtures(gameweek)
        if not any(self._team_fixtures(context) for context in fixtures.values()):
            raise ValueError(f"No fixtures are published for gameweek {gameweek}")
        team_fixtures = result["team_name"].map(
            lambda team: self._team_fixtures(fixtures.get(str(team)))
            if pd.notna(team)
            else []
        )
        result["gameweek"] = gameweek
        result["_fixtures"] = team_fixtures
        result["fixture_count"] = team_fixtures.map(len).astype(int)
        result["opponent_team_name"] = team_fixtures.map(_opponent_label)
        # A double gameweek with a home and an away match has no single venue;
        # its opponent label carries each fixture's venue instead.
        result["was_home"] = team_fixtures.map(_shared_venue)
        result = normalize_fpl_columns(result)

        blank = result["fixture_count"].eq(0)
        if blank.any():
            blank_teams = sorted(
                str(team) for team in result.loc[blank, "team_name"].dropna().unique()
            )
            logger.warning(
                "No gameweek %d fixture for %d players (teams: %s); projecting 0",
                gameweek,
                int(blank.sum()),
                ", ".join(blank_teams) or "unknown",
            )
        return result

    @staticmethod
    def _fixture_model_rows(players: pd.DataFrame) -> pd.DataFrame:
        """Expand players into one model row per fixture they play."""
        positions: List[int] = []
        opponents: List[Any] = []
        venues: List[Any] = []
        for position, fixtures in enumerate(players["_fixtures"]):
            for fixture in fixtures:
                positions.append(position)
                opponents.append(fixture.get("opponent_team_name", np.nan))
                venues.append(fixture.get("was_home", np.nan))

        rows = (
            players.drop(columns=["_fixtures"])
            .iloc[positions]
            .reset_index(drop=True)
        )
        rows["opponent_team_name"] = opponents
        rows["was_home"] = venues
        rows["_player_row"] = positions
        return normalize_fpl_columns(rows)

    @staticmethod
    def _attach_availability(
        players: pd.DataFrame, availability: Optional[pd.DataFrame]
    ) -> pd.DataFrame:
        """Join current official availability (never historical) by player id."""
        result = players.copy()
        if availability is not None and not availability.empty and "id" in result:
            current = (
                availability.loc[
                    :,
                    [
                        column
                        for column in ("id", *AVAILABILITY_COLUMNS)
                        if column in availability.columns
                    ],
                ]
                .drop_duplicates("id")
                .set_index("id")
            )
            ids = pd.to_numeric(result["id"], errors="coerce")
            for column in current.columns:
                result[column] = ids.map(current[column]).to_numpy()
        result["availability_factor"] = availability_factor(result)
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

    def _output_columns(self, *extra: str) -> List[str]:
        return list(
            dict.fromkeys(
                [
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
                    *extra,
                ]
            )
        )

    def _uses_cold_start(self, normalized: pd.DataFrame, gameweek: int) -> bool:
        return (
            self.cold_start_enabled
            and gameweek == 1
            and not self._has_usable_pre_gameweek_data(normalized)
        )

    def _build_model_input(
        self,
        normalized: pd.DataFrame,
        gameweek: int,
        availability: Optional[pd.DataFrame],
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Return players, their fixture rows, and the exact model input."""
        players = prepare_recent_player_features(
            normalized,
            gameweek=gameweek,
            history_window=self.history_window,
        )
        players = self._attach_fixture_context(players, gameweek)
        players = self._attach_availability(players, availability)
        fixture_rows = self._fixture_model_rows(players)
        model_input = ensure_feature_columns(fixture_rows, MODEL_FEATURES)
        populated_features = [
            feature for feature in MODEL_FEATURES if model_input[feature].notna().any()
        ]
        entirely_missing_features = [
            feature for feature in MODEL_FEATURES if model_input[feature].isna().all()
        ]

        logger.info(
            "Generating gameweek %d predictions for %d players across %d fixtures",
            gameweek,
            len(players),
            len(fixture_rows),
        )
        logger.info(
            "Scout inference model features (%d): %s",
            len(MODEL_FEATURES),
            ", ".join(MODEL_FEATURES),
        )
        logger.info(
            "Scout inference feature coverage for gameweek %d: "
            "populated (%d): %s; entirely missing (%d): %s",
            gameweek,
            len(populated_features),
            ", ".join(populated_features) or "none",
            len(entirely_missing_features),
            ", ".join(entirely_missing_features) or "none",
        )
        return players, fixture_rows, model_input

    def predict_players(
        self,
        data: pd.DataFrame,
        gameweek: Optional[int] = None,
        availability: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Generate predictions from an already loaded FPL history frame.

        ``availability`` holds each player's *current* official status. Model
        points are predicted per fixture, summed across a double gameweek,
        and scaled by the probability that the player is available.
        """
        normalized = normalize_fpl_columns(data)
        resolved_gameweek = self._resolve_gameweek(normalized, gameweek)
        if self._uses_cold_start(normalized, resolved_gameweek):
            return self._predict_ownership_cold_start(normalized, availability)

        players, fixture_rows, model_input = self._build_model_input(
            normalized, resolved_gameweek, availability
        )
        ensemble, diagnostics = self._predict_ensemble(model_input)
        diagnostics["strategy"] = "model-ensemble"
        match_points = (
            pd.Series(ensemble, index=fixture_rows["_player_row"].to_numpy())
            .groupby(level=0)
            .sum()
            .reindex(range(len(players)), fill_value=0.0)
            .to_numpy()
        )
        players["expected_points"] = (
            match_points * players["availability_factor"].to_numpy()
        )

        output_columns = self._output_columns("fixture_count", "availability_factor")
        for column in output_columns:
            if column not in players.columns:
                players[column] = np.nan
        result = players[output_columns].copy()
        result.attrs["gameweek"] = resolved_gameweek
        result.attrs["inference"] = diagnostics

        self.gameweek = resolved_gameweek
        logger.info(
            "Inference complete with models: %s",
            ", ".join(diagnostics["successful_models"]),
        )
        return result

    @staticmethod
    def _has_usable_pre_gameweek_data(data: pd.DataFrame) -> bool:
        """Return whether GW0 rows contain genuine non-zero match evidence."""
        if "gameweek" not in data.columns:
            return False
        gameweeks = pd.to_numeric(data["gameweek"], errors="coerce")
        prior = data.loc[gameweeks < 1]
        evidence_features = [
            feature
            for feature in HISTORY_FEATURES
            if feature not in {"now_cost", "selected_by_percent"}
            and feature in prior.columns
        ]
        if prior.empty or not evidence_features:
            return False
        evidence = prior[evidence_features].apply(pd.to_numeric, errors="coerce")
        return bool(evidence.fillna(0).ne(0).any(axis=None))

    def _predict_ownership_cold_start(
        self, data: pd.DataFrame, availability: Optional[pd.DataFrame] = None
    ) -> pd.DataFrame:
        """Rank GW1 players from current ownership when no match history exists."""
        required = {"id", "element_type", "web_name", "team_name", "gameweek"}
        missing = sorted(required.difference(data.columns))
        if missing:
            raise ValueError(f"Cold-start data is missing required columns: {missing}")

        gameweeks = pd.to_numeric(data["gameweek"], errors="coerce")
        preseason = data.loc[gameweeks < 1].copy()
        if preseason.empty:
            raise ValueError("No current player roster is available for GW1 cold start")
        preseason["gameweek"] = gameweeks.loc[preseason.index]
        preseason = preseason.sort_values(
            ["id", "gameweek"], ascending=[True, False], kind="stable"
        )
        players = preseason.drop_duplicates("id", keep="first").copy()
        players = self._attach_fixture_context(players, 1)

        if "selected_by_percent" not in players.columns:
            raise InferenceError(
                "GW1 ownership cold start requires selected_by_percent"
            )
        ownership = pd.to_numeric(players["selected_by_percent"], errors="coerce").clip(
            lower=0
        )
        if not ownership.gt(0).any():
            raise InferenceError(
                "GW1 ownership cold start has no positive ownership values"
            )

        players = self._attach_availability(players, availability)
        players["selected_by_percent"] = ownership.fillna(0.0)
        players["cold_start_score"] = (
            np.log1p(players["selected_by_percent"])
            * players["availability_factor"]
            * players["fixture_count"].gt(0)
        )
        # Preserve the API's ranking field while identifying it as a GW1 score
        # in metadata. It is not presented as a model point forecast internally.
        players["expected_points"] = players["cold_start_score"]

        output_columns = self._output_columns(
            "cold_start_score",
            "now_cost",
            "selected_by_percent",
            "status",
            "can_select",
            "chance_of_playing_next_round",
            "fixture_count",
            "availability_factor",
        )
        for column in output_columns:
            if column not in players.columns:
                players[column] = np.nan
        result = players[output_columns].copy()
        result.attrs["gameweek"] = 1
        result.attrs["inference"] = {
            "strategy": "ownership-cold-start",
            "reason": "no-usable-pre-gameweek-history",
            "score": (
                "log1p(selected_by_percent) * availability_factor "
                "* (fixture_count > 0)"
            ),
            "successful_models": [],
            "failed_models": {},
        }
        self.gameweek = 1
        logger.warning(
            "GW1 has no usable pre-gameweek history; using ownership cold start "
            "for %d players (models skipped)",
            len(result),
        )
        logger.info(
            "GW1 cold-start inputs: selected_by_percent, status, can_select, "
            "chance_of_playing_next_round"
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

    def get_official_predictions(
        self,
        gameweek: Optional[int] = None,
        *,
        collect_results: bool = True,
        allow_stale: bool = False,
    ) -> pd.DataFrame:
        """Generate predictions from official history plus guarded enrichment.

        Once a gameweek's deadline has passed, the archived pre-deadline
        forecast is returned unchanged (``attrs["frozen"]``), so recalling an
        old gameweek never re-predicts it with newer data. A closed gameweek
        with no frozen forecast is frozen on its first recall. Open gameweeks
        are predicted live, and the prediction is reused for a few minutes
        (see ``_live_predictions``). With ``allow_stale``, an older forecast
        may be returned with ``attrs["refresh_due"]`` set; the caller then
        asks for ``refresh_predictions`` in a request of its own.
        """
        resolved_gameweek = int(gameweek or self.official_client.next_gameweek())
        frozen = self.data_archive.frozen_forecast(
            self.official_client, resolved_gameweek
        )
        if frozen is not None:
            logger.info(
                "Serving the frozen GW%d forecast captured at %s",
                resolved_gameweek,
                frozen.attrs.get("forecast_captured_at"),
            )
            # The web UI collects results in a separate request after rendering
            # this forecast. Other callers retain synchronous collection.
            if collect_results:
                self.data_archive.collect_results(self.official_client)
            return frozen
        return self._live_predictions(resolved_gameweek, allow_stale=allow_stale)

    def refresh_predictions(self, gameweek: Optional[int] = None) -> str:
        """Predict an open gameweek again if its cached forecast is not fresh.

        Called in its own request after a caller was served a stale or
        restored forecast, so the prediction has the request's CPU. Joins a
        prediction already under way. Returns "fresh" when nothing was due,
        "refreshed" after predicting, or "not-cached" for a gameweek this
        cache does not hold (closed, or caching disabled).
        """
        resolved_gameweek = int(gameweek or self.official_client.next_gameweek())
        if not self._caches(resolved_gameweek):
            return "not-cached"
        cached = self._usable_forecast(resolved_gameweek)
        if cached is not None and self.clock() < cached.fresh_until:
            return "fresh"
        self._predict_once(resolved_gameweek)
        return "refreshed"

    def _caches(self, gameweek: int) -> bool:
        """Whether this gameweek's live forecast is cached: it must be open.

        Once its deadline passes, requests read the frozen forecast, or
        predict again so the gameweek can be frozen (see
        ``DataArchive.freeze_after_deadline``), exactly as without the cache.
        """
        return self.prediction_cache_ttl > 0 and self.data_archive.gameweek_is_open(
            self.official_client, gameweek
        )

    def _live_predictions(self, gameweek: int, *, allow_stale: bool) -> pd.DataFrame:
        """Return a live forecast, predicting at most once at a time.

        A fresh forecast is reused. With ``allow_stale``, one past its TTL
        (within the stale limit), or after a cold start the archived one, is
        returned marked ``refresh_due``. Otherwise the request predicts,
        or waits for the prediction another request has under way.
        """
        if not self._caches(gameweek):
            return self._predict_live(gameweek)
        cached = self._usable_forecast(gameweek)
        if cached is not None:
            fresh = self.clock() < cached.fresh_until
            if fresh or allow_stale:
                return self._served(cached.predictions, refresh_due=not fresh)
        elif allow_stale:
            restored = self._restored_forecast(gameweek)
            if restored is not None:
                return self._served(restored, refresh_due=True)
        return self._served(self._predict_once(gameweek), refresh_due=False)

    @staticmethod
    def _served(predictions: pd.DataFrame, *, refresh_due: bool) -> pd.DataFrame:
        served = _copy_predictions(predictions)
        served.attrs["refresh_due"] = refresh_due
        return served

    def _usable_forecast(self, gameweek: int) -> Optional[_LiveForecast]:
        """The cached forecast, unless it is past the stale limit or official
        availability has changed since it was made.

        Availability (injuries, suspensions, departures) moves projections,
        so a forecast made before such news is dropped and predicted again,
        however recent: news is never older than the official data cache.
        """
        with self._forecast_lock:
            cached = self._live_forecasts.get(gameweek)
            checked = self._availability_checked.get(gameweek)
        if cached is None or self.clock() >= cached.stale_until:
            return None
        # The official client returns the same object until it fetches new
        # data, and a check against unchanged data cannot find news.
        official = self._current_bootstrap()
        if (
            official is not None
            and checked is not None
            and checked[0] is cached
            and checked[1] is official
        ):
            return cached
        if not self._availability_changed(cached.predictions):
            if official is not None:
                with self._forecast_lock:
                    self._availability_checked[gameweek] = (cached, official)
            return cached
        logger.info("Official availability changed; predicting GW%d again", gameweek)
        with self._forecast_lock:
            if self._live_forecasts.get(gameweek) is cached:
                del self._live_forecasts[gameweek]
        return None

    def _current_bootstrap(self) -> Any:
        """The official client's current data, or None if it is unavailable."""
        bootstrap = getattr(self.official_client, "bootstrap", None)
        if not callable(bootstrap):
            return None
        try:
            return bootstrap()
        except Exception:
            return None

    def _availability_changed(self, predictions: pd.DataFrame) -> bool:
        """Whether any player's current official availability factor differs
        from the one the forecast was made with.

        Both come from ``_attach_availability``, so they match exactly while
        official status, chance of playing and selectability are unchanged.
        If current availability cannot be read, the forecast is kept: the
        prediction it would trigger needs the same official data.
        """
        if not {"id", "availability_factor"} <= set(predictions.columns):
            return False
        try:
            current = self._official_availability()
        except Exception as error:
            logger.warning("Could not check official availability: %s", error)
            return False
        if current is None:
            return False
        forecast = predictions[["id", "availability_factor"]].drop_duplicates("id")
        now = self._attach_availability(forecast[["id"]], current)
        made = pd.to_numeric(forecast["availability_factor"], errors="coerce")
        return not np.allclose(
            made.to_numpy(dtype=float),
            now["availability_factor"].to_numpy(dtype=float),
            equal_nan=True,
        )

    def _restored_forecast(self, gameweek: int) -> Optional[pd.DataFrame]:
        """The archived forecast, for an instance that has not predicted this
        gameweek yet (just started), if it is recent, was made by the running
        models, and official availability has not changed since."""
        with self._forecast_lock:
            if gameweek in self._pending_forecasts or gameweek in self._live_forecasts:
                return None
        restored = self.data_archive.recent_open_forecast(
            self.official_client,
            gameweek,
            timedelta(seconds=self.prediction_restore_max_age),
            model_versions=self._model_versions(),
        )
        if restored is None or self._availability_changed(restored):
            return None
        with self._forecast_lock:
            # The archive read ran unlocked: another request may have predicted
            # or started predicting meanwhile, and its forecast wins.
            if gameweek in self._live_forecasts or gameweek in self._pending_forecasts:
                return None
            now = self.clock()
            # Already past its TTL, so the next refresh predicts again.
            self._live_forecasts[gameweek] = _LiveForecast(
                restored, now, now + self.prediction_stale_ttl
            )
        return restored

    def _predict_once(self, gameweek: int) -> pd.DataFrame:
        """Predict in this request, or wait for the request already doing so.

        The cache is checked again under the lock, so a request that reaches
        this after unlocked work (an archive read) reuses a forecast another
        request has just made instead of predicting it twice.
        """
        with self._forecast_lock:
            cached = self._live_forecasts.get(gameweek)
            if cached is not None and self.clock() < cached.fresh_until:
                return cached.predictions
            pending = self._pending_forecasts.get(gameweek)
            owner = pending is None
            if owner:
                pending = Future()
                self._pending_forecasts[gameweek] = pending
        if not owner:
            return pending.result()
        try:
            predictions = self._predict_live(gameweek)
        except BaseException as error:
            with self._forecast_lock:
                self._pending_forecasts.pop(gameweek, None)
            pending.set_exception(error)
            raise
        # Stored as it stops being pending, so no request in between starts
        # the same prediction again.
        with self._forecast_lock:
            self._pending_forecasts.pop(gameweek, None)
            if predictions.attrs.get("frozen"):
                # Frozen on its first recall after the deadline; every later
                # request reads the frozen forecast instead.
                self._live_forecasts.pop(gameweek, None)
            else:
                now = self.clock()
                self._live_forecasts[gameweek] = _LiveForecast(
                    predictions,
                    now + self.prediction_cache_ttl,
                    now + self.prediction_stale_ttl,
                )
        pending.set_result(predictions)
        return predictions

    def _model_versions(self) -> Dict[str, Dict[str, Any]]:
        """The configured models, as each archived forecast records them."""
        return {
            name: {
                key: value
                for key, value in metadata.items()
                if key in {"version", "last_trained", "year", "weight", "path"}
            }
            for name, metadata in self.config.get("models", {}).items()
            if isinstance(metadata, Mapping)
        }

    def _predict_live(self, gameweek: int) -> pd.DataFrame:
        """Predict a gameweek from current official history, then archive it."""
        resolved_gameweek = gameweek
        logger.info("Loading official FPL history for gameweek %d", resolved_gameweek)
        official_history = self.official_client.player_history(resolved_gameweek)
        history = official_history
        logger.info("Loaded %d official FPL history rows", len(official_history))

        enrichment = {
            "provider": "fpl-data",
            "status": "disabled",
        }
        season_problem = (
            self._fpl_data_season_problem() if self.fpl_data_enabled else None
        )
        if self.fpl_data_enabled and resolved_gameweek < self.fpl_data_start_gameweek:
            enrichment["status"] = "before-start-gameweek"
        elif season_problem:
            # Player IDs are reassigned every season, so another season's
            # dataset must never be merged (or even downloaded).
            enrichment = {"provider": "fpl-data", **season_problem}
        elif self.fpl_data_enabled and self.fpl_data_provider is not None:
            try:
                history, enrichment = self.fpl_data_provider.enrich(
                    history, resolved_gameweek
                )
            except Exception as error:
                # Optional enrichment must never make official inference unavailable.
                logger.warning(
                    "FPL Data provider failed; using official history: %s", error
                )
                enrichment = {
                    "provider": "fpl-data",
                    "status": "unavailable",
                    "error": str(error),
                }

        result = self.predict_players(
            history,
            gameweek=resolved_gameweek,
            availability=self._official_availability(),
        )
        self.last_data_enrichment = dict(enrichment)
        result.attrs["frozen"] = False
        result.attrs["inference"]["data_enrichment"] = enrichment
        result.attrs["source"] = (
            "official-fpl+fpl-data"
            if enrichment.get("status") == "applied"
            else "official-fpl"
        )
        archive_result = self.data_archive.capture_inference(
            official_client=self.official_client,
            prediction_gameweek=resolved_gameweek,
            official_history=official_history,
            enriched_history=history,
            predictions=result,
            source=str(result.attrs["source"]),
            enrichment=enrichment,
            model_versions=self._model_versions(),
        )
        result.attrs["archive"] = archive_result
        # A closed gameweek that was never frozen is frozen now, so every
        # later recall returns this same forecast.
        frozen = self.data_archive.freeze_after_deadline(
            self.official_client,
            resolved_gameweek,
            result,
            validate=self.select_optimal_team,
        )
        return result if frozen is None else frozen

    def _fpl_data_season_problem(self) -> Optional[Dict[str, str]]:
        """Explain why FPL Data must not be used for the live season, if so.

        The check fails closed: enrichment runs only once the official season
        is known to match the configured one.
        """

        def unverified(reason: str) -> Dict[str, str]:
            return {
                "status": "season-unverified",
                "error": f"The official FPL season could not be confirmed: {reason}",
            }

        configured = re.match(r"(\d{4})", self.fpl_data_season)
        if configured is None:
            return unverified(
                f"fpl_data_inference.season {self.fpl_data_season!r} "
                "names no start year"
            )
        bootstrap = getattr(self.official_client, "bootstrap", None)
        if not callable(bootstrap):
            return unverified("the official client does not report the season")
        try:
            events = bootstrap().get("events", [])
        except Exception as error:
            return unverified(str(error))
        deadlines = sorted(
            str(event["deadline_time"])
            for event in events
            if event.get("deadline_time")
        )
        if not deadlines or not deadlines[0][:4].isdigit():
            return unverified("official FPL lists no gameweek deadlines")
        official_start = int(deadlines[0][:4])
        if int(configured.group(1)) == official_start:
            return None
        return {
            "status": "season-mismatch",
            "error": (
                f"fpl_data_inference.season {self.fpl_data_season!r} does not "
                f"match the official {official_start}-{official_start + 1} season"
            ),
        }

    def _official_availability(self) -> Optional[pd.DataFrame]:
        """Return current official availability for every player, if supported."""
        mapped_players = getattr(self.official_client, "mapped_players", None)
        if not callable(mapped_players):
            return None
        players = pd.DataFrame(mapped_players())
        if players.empty or "id" not in players.columns:
            return None
        columns = [
            column for column in ("id", *AVAILABILITY_COLUMNS) if column in players
        ]
        return players[columns]

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
        strategy = predictions.attrs.get("inference", {}).get("strategy")
        # Players with zero availability (injured, suspended, left the league)
        # are never selected; the cold start also requires availability data.
        team = self._select_budget_free_squad(
            unique_predictions,
            strategy=strategy or "model-ensemble",
            require_available=strategy == "ownership-cold-start",
        )
        starting = self._starting_xi(team)
        starters = team.loc[starting].sort_values(
            "expected_points", ascending=False, kind="stable"
        )
        captain, vice = starters.index[0], starters.index[1]
        # Every pick has a role: the XI's two best projections take the
        # armbands, the rest of the XI is "", and the other four are "bench".
        team["role"] = "bench"
        team.loc[starting, "role"] = ""
        team.loc[captain, "role"] = "captain"
        team.loc[vice, "role"] = "vice"
        team["element_type"] = team["element_type"].map(self.POSITION_MAPPING)

        logger.info(
            "Selected captain %s (%.2f) and vice-captain %s (%.2f); "
            "starting XI %.2f, bench %.2f",
            team.loc[captain, "web_name"],
            team.loc[captain, "expected_points"],
            team.loc[vice, "web_name"],
            team.loc[vice, "expected_points"],
            starters["expected_points"].sum(),
            team.loc[team["role"].eq("bench"), "expected_points"].sum(),
        )
        return team

    @staticmethod
    def _starting_xi(team: pd.DataFrame) -> List[Any]:
        """Return the index of the squad's highest-projected legal XI.

        One goalkeeper, 3–5 defenders, 2–5 midfielders and 1–3 forwards,
        chosen the way the team rating's AI benchmark chooses them.
        """
        positions = pd.to_numeric(team["element_type"], errors="coerce")
        points = pd.to_numeric(team["expected_points"], errors="coerce")
        rows = [
            {
                "element_type": int(positions[index]),
                "expected_points": float(points[index]),
                "_row": index,
            }
            for index in team.index
        ]
        lineup, _ = _best_benchmark_lineup(rows)
        starting = {player["_row"] for player in lineup}
        return [index for index in team.index if index in starting]

    def _select_budget_free_squad(
        self,
        predictions: pd.DataFrame,
        strategy: str,
        require_available: bool = False,
    ) -> pd.DataFrame:
        """Build a budget-free squad with position and per-club limits."""
        required = {"team_name"}
        if require_available:
            required.add("availability_factor")
        missing = sorted(required.difference(predictions.columns))
        if missing:
            raise ValueError(
                f"Squad selection data is missing required columns: {missing}"
            )

        candidates = predictions.copy()
        candidates["_position"] = pd.to_numeric(
            candidates["element_type"], errors="coerce"
        )
        candidates["_score"] = pd.to_numeric(
            candidates["expected_points"], errors="coerce"
        )
        eligible = candidates["_position"].isin(self.TEAM_SELECTION) & candidates[
            "_score"
        ].notna()
        internal_columns = ["_position", "_score"]
        if "availability_factor" in candidates.columns:
            candidates["_availability"] = pd.to_numeric(
                candidates["availability_factor"], errors="coerce"
            ).fillna(0.0 if require_available else 1.0)
            eligible &= candidates["_availability"].gt(0)
            internal_columns.append("_availability")
        candidates = candidates.loc[eligible & candidates["team_name"].notna()].copy()

        for position, count in self.TEAM_SELECTION.items():
            available = int(candidates["_position"].eq(position).sum())
            if available < count:
                raise ValueError(
                    f"Need {count} position-{position} players, found {available}"
                )

        selected: List[int] = []
        position_counts = {position: 0 for position in self.TEAM_SELECTION}
        team_counts: Dict[str, int] = {}
        ranked = candidates.sort_values("_score", ascending=False, kind="stable")
        for index, player in ranked.iterrows():
            position = int(player["_position"])
            team_name = str(player["team_name"])
            if position_counts[position] >= self.TEAM_SELECTION[position]:
                continue
            if team_counts.get(team_name, 0) >= self.max_players_per_team:
                continue
            selected.append(index)
            position_counts[position] += 1
            team_counts[team_name] = team_counts.get(team_name, 0) + 1
            if len(selected) == sum(self.TEAM_SELECTION.values()):
                break

        if any(
            position_counts[position] != count
            for position, count in self.TEAM_SELECTION.items()
        ):
            raise ValueError(
                "Could not construct a Scout squad within the per-team player limit"
            )

        team = (
            candidates.loc[selected]
            .drop(columns=internal_columns, errors="ignore")
            .sort_values("expected_points", ascending=False)
            .reset_index(drop=True)
        )
        logger.info(
            "Selected budget-free %s squad: %d clubs, "
            "maximum %d players per club (price ignored)",
            strategy,
            team["team_name"].nunique(),
            int(team["team_name"].value_counts().max()),
        )
        return team


if __name__ == "__main__":
    from src.utils import load_config

    configuration = load_config("config/config.yaml")
    scout = FPLScout(configuration)
    player_predictions = scout.get_official_predictions(gameweek=1)
    print(scout.select_optimal_team(player_predictions))

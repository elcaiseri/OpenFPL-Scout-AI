"""Guarded FPL Data enrichment for official-FPL inference history."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Optional

import pandas as pd

from scripts.download_fpl_data import (
    SOURCE_PAGE,
    TARGET_FEATURES,
    DatasetSummary,
    FPLDataClient,
    Season,
    validate_csv,
)
from src.features import normalize_fpl_columns
from src.logger import get_logger

logger = get_logger(__name__)

MATCH_KEYS = ("id", "gameweek", "opponent_team_name", "was_home")


class FPLDataInferenceError(RuntimeError):
    """Raised when FPL Data cannot safely enrich official history."""


@dataclass(frozen=True)
class _LoadedDataset:
    frame: pd.DataFrame
    summary: DatasetSummary
    origin: str


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _metadata_path(path: Path) -> Path:
    return path.with_suffix(".metadata.json")


def _home_key(value: Any) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return "1"
    if normalized in {"false", "0", "no"}:
        return "0"
    return ""


def _with_match_keys(frame: pd.DataFrame) -> pd.DataFrame:
    keyed = normalize_fpl_columns(frame).reset_index(drop=True)
    missing = sorted(set(MATCH_KEYS).difference(keyed.columns))
    if missing:
        raise FPLDataInferenceError(f"History is missing match keys: {missing}")
    keyed["_fpl_data_id"] = pd.to_numeric(keyed["id"], errors="coerce").astype("Int64")
    keyed["_fpl_data_gameweek"] = pd.to_numeric(
        keyed["gameweek"], errors="coerce"
    ).astype("Int64")
    keyed["_fpl_data_opponent"] = (
        keyed["opponent_team_name"].astype("string").str.strip().str.casefold()
    )
    keyed["_fpl_data_home"] = keyed["was_home"].map(_home_key)
    return keyed


class FPLDataHistoryProvider:
    """Cache, validate, and merge one explicitly configured FPL Data season."""

    def __init__(
        self,
        season_value: str,
        *,
        local_path: Optional[Path] = None,
        runtime_cache_path: Optional[Path] = None,
        refresh_ttl_seconds: int = 21600,
        minimum_match_ratio: float = 0.8,
        timeout_seconds: float = 60.0,
        permission_status: str = "pending",
        client: Optional[FPLDataClient] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not season_value:
            raise ValueError("fpl_data_inference.season must be explicit")
        if refresh_ttl_seconds < 60:
            raise ValueError(
                "fpl_data_inference.refresh_ttl_seconds must be at least 60"
            )
        if not 0 < minimum_match_ratio <= 1:
            raise ValueError(
                "fpl_data_inference.minimum_match_ratio must be between 0 and 1"
            )
        self.season_value = season_value
        self.local_path = Path(local_path) if local_path else None
        self.runtime_cache_path = (
            Path(runtime_cache_path) if runtime_cache_path else None
        )
        self.refresh_ttl_seconds = int(refresh_ttl_seconds)
        self.minimum_match_ratio = float(minimum_match_ratio)
        self.permission_status = permission_status
        self.client = client or FPLDataClient(timeout_seconds=timeout_seconds)
        self.clock = clock

        self._lock = RLock()
        self._dataset: Optional[_LoadedDataset] = None
        self._last_attempt_at: Optional[float] = None
        self._last_error: Optional[str] = None

    def _read_local(self, path: Path) -> _LoadedDataset:
        metadata_file = _metadata_path(path)
        if not path.is_file() or not metadata_file.is_file():
            raise FPLDataInferenceError(
                f"Local FPL Data cache or metadata is missing: {path}"
            )
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            metadata_season = metadata["season"]["value"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FPLDataInferenceError(
                f"Local FPL Data metadata is invalid: {metadata_file}"
            ) from exc
        if metadata_season != self.season_value:
            raise FPLDataInferenceError(
                f"Local FPL Data season {metadata_season!r} does not match "
                f"{self.season_value!r}"
            )

        raw = path.read_bytes()
        summary = validate_csv(raw)
        recorded_digest = metadata.get("sha256")
        if recorded_digest and recorded_digest != summary.sha256:
            raise FPLDataInferenceError(
                f"Local FPL Data checksum does not match metadata: {path}"
            )
        return _LoadedDataset(
            frame=normalize_fpl_columns(pd.read_csv(io.BytesIO(raw))),
            summary=summary,
            origin=f"local:{path}",
        )

    def _write_runtime_cache(
        self, raw: bytes, season: Season, summary: DatasetSummary
    ) -> None:
        if self.runtime_cache_path is None:
            return
        metadata = {
            "cached_at_utc": datetime.now(timezone.utc).isoformat(),
            "license_status": f"permission-{self.permission_status}",
            "season": asdict(season),
            "sha256": summary.sha256,
            "source_page": SOURCE_PAGE,
        }
        _atomic_write(self.runtime_cache_path, raw)
        _atomic_write(
            _metadata_path(self.runtime_cache_path),
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    def _download(self) -> _LoadedDataset:
        seasons = self.client.available_seasons()
        season = next(
            (item for item in seasons if item.value == self.season_value), None
        )
        if season is None:
            offered = ", ".join(item.value for item in seasons) or "none"
            raise FPLDataInferenceError(
                f"FPL Data does not offer configured season {self.season_value}; "
                f"offered: {offered}"
            )
        raw, _ = self.client.download_csv(season)
        summary = validate_csv(raw)
        frame = normalize_fpl_columns(pd.read_csv(io.BytesIO(raw)))
        try:
            self._write_runtime_cache(raw, season, summary)
        except OSError as error:
            logger.warning("Could not persist the FPL Data runtime cache: %s", error)
        return _LoadedDataset(frame=frame, summary=summary, origin="remote")

    def _fallback_local(self) -> _LoadedDataset:
        errors = []
        candidates = (self.runtime_cache_path, self.local_path)
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                return self._read_local(candidate)
            except (FPLDataInferenceError, OSError, ValueError) as error:
                errors.append(str(error))
        detail = "; ".join(errors) if errors else "no local cache configured"
        raise FPLDataInferenceError(detail)

    def _load(self) -> tuple[_LoadedDataset, str]:
        now = self.clock()
        with self._lock:
            if (
                self._dataset is not None
                and self._last_attempt_at is not None
                and now - self._last_attempt_at < self.refresh_ttl_seconds
            ):
                return self._dataset, "memory"
            if (
                self._dataset is None
                and self._last_error is not None
                and self._last_attempt_at is not None
                and now - self._last_attempt_at < self.refresh_ttl_seconds
            ):
                raise FPLDataInferenceError(
                    f"Cached FPL Data failure: {self._last_error}"
                )

            self._last_attempt_at = now
            try:
                dataset = self._download()
            except Exception as download_error:
                self._last_error = str(download_error)
                if self._dataset is not None:
                    return self._dataset, "stale-memory-fallback"
                try:
                    dataset = self._fallback_local()
                except FPLDataInferenceError as local_error:
                    raise FPLDataInferenceError(
                        f"Remote load failed ({download_error}); local fallback "
                        f"failed ({local_error})"
                    ) from download_error
                self._dataset = dataset
                return dataset, "local-fallback"

            self._dataset = dataset
            self._last_error = None
            return dataset, "remote"

    def enrich(
        self, official_history: pd.DataFrame, target_gameweek: int
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Fill missing audited features without overriding official values."""
        diagnostics: dict[str, Any] = {
            "provider": "fpl-data",
            "permission_status": self.permission_status,
            "season": self.season_value,
        }
        try:
            dataset, cache_status = self._load()
            diagnostics["cache"] = cache_status
            diagnostics["dataset_sha256"] = dataset.summary.sha256

            official = _with_match_keys(official_history)
            external = _with_match_keys(dataset.frame)
            external = external.loc[
                pd.to_numeric(external.get("element_type"), errors="coerce").isin(
                    [1, 2, 3, 4]
                )
            ].copy()

            eligible = (
                official["_fpl_data_gameweek"].notna()
                & (official["_fpl_data_gameweek"] >= 1)
                & (official["_fpl_data_gameweek"] < int(target_gameweek))
            )
            eligible_rows = int(eligible.sum())
            diagnostics["eligible_rows"] = eligible_rows
            if eligible_rows == 0:
                diagnostics["status"] = "no-played-history"
                return official_history.copy(), diagnostics

            required_gameweek = int(official.loc[eligible, "_fpl_data_gameweek"].max())
            diagnostics["required_history_gameweek"] = required_gameweek
            diagnostics["source_observed_gameweek"] = (
                dataset.summary.latest_observed_gameweek
            )
            if dataset.summary.latest_observed_gameweek < required_gameweek:
                diagnostics["status"] = "stale"
                diagnostics["error"] = (
                    "FPL Data has not caught up with official history "
                    f"({dataset.summary.latest_observed_gameweek} < "
                    f"{required_gameweek})"
                )
                return official_history.copy(), diagnostics

            available_features = [
                feature
                for feature in TARGET_FEATURES
                if feature in external.columns and external[feature].notna().any()
            ]
            diagnostics["supplied_features"] = available_features
            diagnostics["missing_features"] = [
                feature
                for feature in TARGET_FEATURES
                if feature not in available_features
            ]
            if not available_features:
                diagnostics["status"] = "no-features"
                return official_history.copy(), diagnostics

            helper_keys = [
                "_fpl_data_id",
                "_fpl_data_gameweek",
                "_fpl_data_opponent",
                "_fpl_data_home",
            ]
            external = external.loc[
                (external["_fpl_data_gameweek"] >= 1)
                & (external["_fpl_data_gameweek"] < int(target_gameweek)),
                [*helper_keys, *available_features],
            ].copy()
            duplicate_keys = int(external.duplicated(helper_keys, keep=False).sum())
            if duplicate_keys:
                raise FPLDataInferenceError(
                    f"FPL Data has {duplicate_keys} rows with duplicate match keys"
                )
            rename = {
                feature: f"_fpl_data_value_{feature}" for feature in available_features
            }
            external = external.rename(columns=rename)

            official["_fpl_data_order"] = range(len(official))
            merged = official.merge(
                external,
                how="left",
                on=helper_keys,
                sort=False,
                validate="many_to_one",
                indicator="_fpl_data_match",
            )
            matched = eligible & merged["_fpl_data_match"].eq("both")
            matched_rows = int(matched.sum())
            match_ratio = matched_rows / eligible_rows
            diagnostics["matched_rows"] = matched_rows
            diagnostics["match_ratio"] = round(match_ratio, 6)
            if match_ratio < self.minimum_match_ratio:
                diagnostics["status"] = "rejected-low-match-ratio"
                diagnostics["error"] = (
                    f"Only {matched_rows}/{eligible_rows} official history rows "
                    "matched FPL Data"
                )
                return official_history.copy(), diagnostics

            filled_values = {}
            for feature in available_features:
                external_column = rename[feature]
                source_values = pd.to_numeric(merged[external_column], errors="coerce")
                if feature in merged.columns:
                    official_values = pd.to_numeric(merged[feature], errors="coerce")
                else:
                    official_values = pd.Series(float("nan"), index=merged.index)
                filled_values[feature] = int(
                    (official_values.isna() & source_values.notna()).sum()
                )
                merged[feature] = official_values.mask(
                    official_values.isna(), source_values
                )

            drop_columns = [
                *helper_keys,
                "_fpl_data_order",
                "_fpl_data_match",
                *rename.values(),
            ]
            merged = merged.sort_values("_fpl_data_order", kind="stable").drop(
                columns=drop_columns
            )
            diagnostics["filled_values"] = filled_values
            diagnostics["status"] = "applied"
            return merged.reset_index(drop=True), diagnostics
        except Exception as error:
            logger.warning("FPL Data enrichment unavailable: %s", error)
            diagnostics["status"] = "unavailable"
            diagnostics["error"] = str(error)
            return official_history.copy(), diagnostics

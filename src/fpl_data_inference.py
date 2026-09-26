"""Guarded FPL Data enrichment for official-FPL inference history."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition
from typing import Any, Callable, Optional

import pandas as pd

from scripts.download_fpl_data import (
    SOURCE_PAGE,
    TARGET_FEATURES,
    DatasetSummary,
    FPLDataClient,
    Season,
    atomic_write_pair,
    check_for_regression,
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
        max_gameweek_lag: int = 1,
        timeout_seconds: float = 60.0,
        permission_status: str = "pending",
        acknowledge_permission_pending: bool = False,
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
        if max_gameweek_lag < 0:
            raise ValueError("fpl_data_inference.max_gameweek_lag must be at least 0")
        self.season_value = season_value
        self.local_path = Path(local_path) if local_path else None
        self.runtime_cache_path = (
            Path(runtime_cache_path) if runtime_cache_path else None
        )
        self.refresh_ttl_seconds = int(refresh_ttl_seconds)
        self.minimum_match_ratio = float(minimum_match_ratio)
        # FPL Data publishes a gameweek after Official FPL already shows it
        # (live fixtures appear in official history as they are played).
        # Within this lag the covered gameweeks are enriched and the newest
        # stay official-only; a source further behind is rejected as stale.
        self.max_gameweek_lag = int(max_gameweek_lag)
        self.permission_status = permission_status
        # Like the import CLI's --acknowledge-permission-pending flag, the
        # service contacts FPL Data only when reuse permission is granted or
        # an operator explicitly accepts the pending status. Otherwise it
        # reads locally imported files only.
        self.remote_download_allowed = (
            permission_status.strip().casefold() == "granted"
            or bool(acknowledge_permission_pending)
        )
        self.client = client or FPLDataClient(timeout_seconds=timeout_seconds)
        self.clock = clock

        self._condition = Condition()
        self._refreshing = False
        self._dataset: Optional[_LoadedDataset] = None
        self._last_attempt_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_refresh_error: Optional[str] = None

    def _read_local_source(self, path: Path) -> tuple[bytes, DatasetSummary]:
        """Return a local copy's bytes after season, schema, and checksum checks."""
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
        return raw, summary

    def _read_local(self, path: Path) -> _LoadedDataset:
        raw, summary = self._read_local_source(path)
        return _LoadedDataset(
            frame=normalize_fpl_columns(pd.read_csv(io.BytesIO(raw))),
            summary=summary,
            origin=f"local:{path}",
        )

    def _write_runtime_cache(
        self,
        raw: bytes,
        season: Season,
        summary: DatasetSummary,
        source_filename: str,
    ) -> None:
        if self.runtime_cache_path is None:
            return
        path = self.runtime_cache_path
        metadata_file = _metadata_path(path)
        # Most refreshes download identical bytes; never rewrite a large file
        # on the shared data volume when the cached copy already matches.
        try:
            data_matches = hashlib.sha256(path.read_bytes()).hexdigest() == (
                summary.sha256
            )
        except OSError:
            data_matches = False
        try:
            existing = json.loads(metadata_file.read_text(encoding="utf-8"))
            metadata_matches = (
                existing.get("sha256") == summary.sha256
                and existing.get("season", {}).get("value") == season.value
                and existing.get("source_filename") == source_filename
            )
        except (OSError, ValueError, AttributeError):
            metadata_matches = False
        if data_matches and metadata_matches:
            return
        metadata = {
            "access_method": "public Download CSV button (Dash callback)",
            "cached_at_utc": datetime.now(timezone.utc).isoformat(),
            "license_status": f"permission-{self.permission_status}",
            "season": asdict(season),
            "source_filename": source_filename,
            "source_page": SOURCE_PAGE,
            "status": "runtime-refresh",
            **asdict(summary),
        }
        metadata_bytes = (
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if data_matches:
            # Repair or upgrade the provenance of an unchanged file.
            _atomic_write(metadata_file, metadata_bytes)
        else:
            atomic_write_pair(path, raw, metadata_file, metadata_bytes)

    def _current_summary(self) -> Optional[DatasetSummary]:
        """Return the dataset a download would replace, if one exists."""
        if self._dataset is not None:
            return self._dataset.summary
        for candidate in (self.runtime_cache_path, self.local_path):
            if candidate is None:
                continue
            try:
                return self._read_local_source(candidate)[1]
            except (FPLDataInferenceError, OSError, ValueError):
                continue
        return None

    def _download(self, current: Optional[DatasetSummary] = None) -> _LoadedDataset:
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
        raw, source_filename = self.client.download_csv(season)
        summary = validate_csv(raw)
        if current is not None:
            # A season's dataset only grows, so an unattended refresh may not
            # replace the dataset in use (or the imported file) with a smaller
            # one. A download that adds a newer gameweek may drop up to 5% of
            # rows or players (a departed player, say), or enrichment would
            # stay on the old dataset for the rest of the season.
            advances = (
                summary.latest_observed_gameweek > current.latest_observed_gameweek
            )
            check_for_regression(
                current, summary, minimum_ratio=0.95 if advances else 1.0
            )
        frame = normalize_fpl_columns(pd.read_csv(io.BytesIO(raw)))
        try:
            self._write_runtime_cache(raw, season, summary, source_filename)
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

    def _fetch(self) -> tuple[_LoadedDataset, str]:
        """Load the dataset from FPL Data when allowed, else from local files.

        After a failed download the local copy is used only when no dataset
        is loaded yet; otherwise the caller keeps the dataset already in use.
        """
        if self.remote_download_allowed:
            try:
                dataset = self._download(self._current_summary())
            except Exception as error:
                remote_error = str(error)
                self._last_refresh_error = remote_error
                logger.warning("FPL Data refresh failed: %s", remote_error)
                if self._dataset is not None:
                    # Keep the dataset in use; the local copy may be older.
                    raise FPLDataInferenceError(remote_error) from error
            else:
                self._last_refresh_error = None
                return dataset, "remote"
            origin = "local-fallback"
        else:
            remote_error = (
                f"remote downloads are disabled while permission is "
                f"{self.permission_status!r} and not acknowledged"
            )
            origin = "local"
        try:
            return self._fallback_local(), origin
        except FPLDataInferenceError as local_error:
            raise FPLDataInferenceError(
                f"Remote load failed ({remote_error}); local fallback "
                f"failed ({local_error})"
            ) from local_error

    def _load(self) -> tuple[_LoadedDataset, str]:
        """Return the dataset, refreshing it at most once per TTL.

        Only one request refreshes at a time. While it runs, other requests
        use the previous dataset instead of waiting on the download; they
        wait only when no dataset has been loaded yet.
        """
        with self._condition:
            while True:
                now = self.clock()
                fresh = (
                    self._last_attempt_at is not None
                    and now - self._last_attempt_at < self.refresh_ttl_seconds
                )
                if self._dataset is not None and fresh:
                    return self._dataset, "memory"
                if self._dataset is None and self._last_error is not None and fresh:
                    raise FPLDataInferenceError(
                        f"Cached FPL Data failure: {self._last_error}"
                    )
                if not self._refreshing:
                    break
                if self._dataset is not None:
                    return self._dataset, "stale-memory-refreshing"
                self._condition.wait()
            self._refreshing = True

        dataset: Optional[_LoadedDataset] = None
        try:
            dataset, origin = self._fetch()
        except Exception as error:
            with self._condition:
                self._last_error = str(error)
                if self._dataset is not None:
                    return self._dataset, "stale-memory-fallback"
            raise
        finally:
            with self._condition:
                if dataset is not None:
                    self._dataset = dataset
                    self._last_error = None
                self._last_attempt_at = self.clock()
                self._refreshing = False
                self._condition.notify_all()
        return dataset, origin

    def enrich(
        self, official_history: pd.DataFrame, target_gameweek: int
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Fill missing audited features without overriding official values."""
        diagnostics: dict[str, Any] = {
            "provider": "fpl-data",
            "permission_status": self.permission_status,
            "remote_download_allowed": self.remote_download_allowed,
            "season": self.season_value,
        }
        try:
            dataset, cache_status = self._load()
            diagnostics["cache"] = cache_status
            diagnostics["dataset_sha256"] = dataset.summary.sha256
            if self._last_refresh_error:
                diagnostics["refresh_error"] = self._last_refresh_error

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
            latest_gameweek = int(dataset.summary.latest_observed_gameweek)
            diagnostics["required_history_gameweek"] = required_gameweek
            diagnostics["source_observed_gameweek"] = latest_gameweek
            if required_gameweek - latest_gameweek > self.max_gameweek_lag:
                diagnostics["status"] = "stale"
                diagnostics["error"] = (
                    "FPL Data has not caught up with official history "
                    f"({latest_gameweek} < {required_gameweek}; at most "
                    f"{self.max_gameweek_lag} gameweek(s) of lag allowed)"
                )
                return official_history.copy(), diagnostics

            # Rows after the source's latest played gameweek stay official-only;
            # FPL Data rows for them may be unplayed placeholders.
            covered = eligible & (official["_fpl_data_gameweek"] <= latest_gameweek)
            covered_rows = int(covered.sum())
            diagnostics["covered_rows"] = covered_rows
            diagnostics["unenriched_gameweeks"] = sorted(
                int(gameweek)
                for gameweek in official.loc[
                    eligible & ~covered, "_fpl_data_gameweek"
                ].unique()
            )
            if covered_rows == 0:
                diagnostics["status"] = "stale"
                diagnostics["error"] = (
                    "FPL Data does not cover any played official gameweek yet"
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
                & (external["_fpl_data_gameweek"] < int(target_gameweek))
                & (external["_fpl_data_gameweek"] <= latest_gameweek),
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
            is_match = merged["_fpl_data_match"].eq("both")
            matched = covered & is_match
            matched_rows = int(matched.sum())
            match_ratio = matched_rows / covered_rows
            diagnostics["matched_rows"] = matched_rows
            diagnostics["match_ratio"] = round(match_ratio, 6)
            unmatched = covered & ~is_match
            if unmatched.any():
                # A spelling mismatch in one club's name shows up here as a
                # single dominant opponent instead of silently missing rows.
                counts = merged.loc[unmatched, "_fpl_data_opponent"].value_counts()
                diagnostics["unmatched_opponents"] = {
                    str(opponent): int(count)
                    for opponent, count in counts.head(5).items()
                }
            if match_ratio < self.minimum_match_ratio:
                diagnostics["status"] = "rejected-low-match-ratio"
                diagnostics["error"] = (
                    f"Only {matched_rows}/{covered_rows} covered official history "
                    "rows matched FPL Data"
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

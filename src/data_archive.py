"""Durable, fail-open archives for future OpenFPL model training."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Optional

import numpy as np
import pandas as pd

from src.logger import get_logger

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


def _json_safe(value: Any) -> Any:
    """Convert pandas/numpy values into strict JSON-compatible values."""
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _parse_deadline(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def training_safe_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep capture-time ownership out of historical match rows.

    Official history rows carry the player's ownership *when the archive was
    captured*, not when the match was played. Training on it would leak
    future information, so played rows leave ``selected_by_percent`` empty and
    the capture-time value moves to ``selected_by_percent_at_capture``. The
    point-in-time manager count remains available as ``official_selected``.
    """
    if "selected_by_percent" not in frame.columns:
        return frame
    result = frame.copy()
    result["selected_by_percent_at_capture"] = result["selected_by_percent"]
    if "gameweek" in result.columns:
        played = pd.to_numeric(result["gameweek"], errors="coerce").ge(1)
    else:
        played = pd.Series(True, index=result.index)
    result["selected_by_percent"] = result["selected_by_percent"].mask(played)
    return result


class DataArchive:
    """Write reproducible gameweek artifacts to a mounted data directory.

    Writes are deliberately fail-open: prediction requests continue if an
    archive bucket is temporarily unavailable. Per-gameweek prediction
    artifacts are written only while that gameweek is still open (before its
    deadline), so the archive keeps the last pre-deadline forecast and never
    changes one that is already being served as frozen. Cumulative official
    history is refreshed on every capture, and live event files become
    immutable shortly after Official FPL marks them ``data_checked``.

    Written for one service instance: a single lock orders forecast writes
    and frozen reads.
    """

    SCHEMA_VERSION = 2
    # The official client caches responses for minutes, so a live payload
    # fetched just after ``data_checked`` may predate the final data. Live
    # files keep being refreshed for this long after the check is first seen.
    LIVE_SETTLE = timedelta(minutes=30)

    def __init__(
        self,
        root_path: Path,
        *,
        enabled: bool = True,
        configured_season: str = "auto",
        clock: Optional[Callable[[], datetime]] = None,
        results_interval_seconds: float = 300.0,
    ) -> None:
        self.root_path = Path(root_path)
        self.enabled = bool(enabled)
        self.configured_season = configured_season.strip() or "auto"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.results_interval = timedelta(seconds=float(results_interval_seconds))
        self._lock = RLock()
        self._digests: dict[Path, str] = {}
        self._squad_digests: dict[Path, str] = {}
        self._results_due_at: Optional[datetime] = None
        self.last_result: dict[str, Any] = {"status": "not-attempted"}

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "DataArchive":
        archive_config = config.get("data_archive", {})
        configured_enabled = bool(archive_config.get("enabled", False))
        environment_enabled = _environment_bool("OPENFPL_DATA_ARCHIVE_ENABLED")
        enabled = (
            configured_enabled
            if environment_enabled is None
            else environment_enabled
        )
        root = os.environ.get("OPENFPL_DATA_ARCHIVE_ROOT")
        if root is None:
            data_root = os.environ.get("OPENFPL_DATA_ROOT")
            root = (
                str(Path(data_root) / "archive")
                if data_root
                else str(archive_config.get("root_path", "data/archive"))
            )
        return cls(
            Path(root),
            enabled=enabled,
            configured_season=str(archive_config.get("season", "auto")),
            results_interval_seconds=float(
                archive_config.get("results_interval_seconds", 300)
            ),
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "root_path": str(self.root_path),
            "last_result": dict(self.last_result),
        }

    def capture_inference(
        self,
        *,
        official_client: Any,
        prediction_gameweek: int,
        official_history: pd.DataFrame,
        enriched_history: pd.DataFrame,
        predictions: pd.DataFrame,
        source: str,
        enrichment: Mapping[str, Any],
        model_versions: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist official inputs, enriched inputs, and model predictions."""
        if not self.enabled:
            return {"status": "disabled"}

        try:
            result = self._capture_inference(
                official_client=official_client,
                prediction_gameweek=int(prediction_gameweek),
                official_history=official_history,
                enriched_history=enriched_history,
                predictions=predictions,
                source=source,
                enrichment=enrichment,
                model_versions=model_versions,
            )
        except Exception as error:  # Archive availability must not break inference.
            logger.exception("Could not persist the gameweek data archive")
            result = {"status": "failed", "error": str(error)}
        else:
            with self._lock:
                self._results_due_at = self.clock() + self.results_interval
        self.last_result = result
        return result

    def collect_results(self, official_client: Any) -> dict[str, Any]:
        """Archive official results without a prediction, once per interval.

        Serving a frozen forecast skips inference, which is what normally
        collects results. This keeps the training archive complete, including
        after the final deadline, when every request is served frozen.
        """
        if not self.enabled:
            return {"status": "disabled"}
        with self._lock:
            now = self.clock()
            if self._results_due_at is not None and now < self._results_due_at:
                return {"status": "throttled"}
            self._results_due_at = now + self.results_interval
        try:
            bootstrap = official_client.bootstrap()
            season = self._season_name(bootstrap)
            season_root = self.root_path / season
            written: list[str] = []
            history_cutoff_gameweek, _ = self._capture_official_history(
                official_client, bootstrap, season_root, written
            )
            live_gameweeks = self._archive_live_gameweeks(
                official_client, bootstrap, season_root, written
            )
            result = {
                "status": "saved",
                "season": season,
                "results_only": True,
                "official_history_before_gameweek": history_cutoff_gameweek,
                "live_gameweeks": live_gameweeks,
                "files_updated": written,
            }
            logger.info(
                "Archived official results under %s (%d files updated)",
                season_root,
                len(written),
            )
        except Exception as error:  # Archive availability must not break the API.
            logger.exception("Could not archive official results")
            result = {"status": "failed", "error": str(error)}
        self.last_result = result
        return result

    def _capture_inference(
        self,
        *,
        official_client: Any,
        prediction_gameweek: int,
        official_history: pd.DataFrame,
        enriched_history: pd.DataFrame,
        predictions: pd.DataFrame,
        source: str,
        enrichment: Mapping[str, Any],
        model_versions: Mapping[str, Any],
    ) -> dict[str, Any]:
        bootstrap = official_client.bootstrap()
        season = self._season_name(bootstrap)
        season_root = self.root_path / season
        gameweek_name = f"gw_{prediction_gameweek:02d}"
        enriched_history = training_safe_history(enriched_history)

        written: list[str] = []
        (
            history_cutoff_gameweek,
            complete_official_history,
        ) = self._capture_official_history(
            official_client, bootstrap, season_root, written, official_history
        )
        if enrichment.get("status") == "applied":
            # A failed or skipped enrichment must not replace enriched files
            # with official-only rows, nor may gameweeks FPL Data has not
            # published yet.
            self._record_gameweek_frames(
                written,
                season_root,
                season_root / "enriched" / "player-stats",
                enriched_history,
                skip_gameweeks={
                    int(gameweek)
                    for gameweek in enrichment.get("unenriched_gameweeks", [])
                },
            )
        live_gameweeks = self._archive_live_gameweeks(
            official_client, bootstrap, season_root, written
        )

        open_gameweek = self._open_gameweek(bootstrap)
        deadline = self._gameweek_deadline(bootstrap, prediction_gameweek)
        skipped_reason = None
        if prediction_gameweek != open_gameweek:
            skipped_reason = (
                "no-open-gameweek"
                if open_gameweek is None
                else f"gameweek-not-open (open: {open_gameweek})"
            )
        else:
            snapshot_root = season_root / "official" / "snapshots" / gameweek_name
            enriched_path = (
                season_root / "enriched" / "history" / f"before_{gameweek_name}.csv"
            )
            predictions_path = season_root / "predictions" / f"{gameweek_name}.csv"
            predictions_csv = predictions.to_csv(index=False).encode("utf-8")
            # Staged and committed together once the deadline is re-checked.
            inputs = {
                snapshot_root / "bootstrap.json": self._json_bytes(bootstrap),
                snapshot_root / "fixtures.json": self._json_bytes(
                    official_client.fixtures()
                ),
                enriched_path: enriched_history.to_csv(index=False).encode("utf-8"),
            }
            metadata = {
                "archive_schema_version": self.SCHEMA_VERSION,
                "predictions_sha256": hashlib.sha256(predictions_csv).hexdigest(),
                # The forecast served once the deadline passes. Kept in this
                # file so it is replaced in one atomic write with its capture
                # time; the predictions CSV is the training record.
                "forecast": self._forecast_payload(predictions),
                "season": season,
                "prediction_gameweek": prediction_gameweek,
                "official_history_before_gameweek": history_cutoff_gameweek,
                "official_total_players": bootstrap.get("total_players"),
                "ownership_note": (
                    "Played history rows leave selected_by_percent empty; "
                    "selected_by_percent_at_capture is ownership at capture "
                    "time and official_selected is the point-in-time count."
                ),
                "source": source,
                "rows": {
                    "official_history": len(complete_official_history),
                    "enriched_history": len(enriched_history),
                    "predictions": len(predictions),
                },
                "live_gameweeks": live_gameweeks,
                "enrichment": enrichment,
                "inference": predictions.attrs.get("inference", {}),
                "models": model_versions,
            }
            if not self._commit_forecast(
                written,
                season_root,
                inputs,
                predictions_path,
                predictions_csv,
                season_root / "metadata" / f"{gameweek_name}.json",
                metadata,
                deadline,
            ):
                skipped_reason = "gameweek-closed"

        prediction_archived = skipped_reason is None
        result = {
            "status": "saved",
            "season": season,
            "prediction_gameweek": prediction_gameweek,
            "prediction_archived": prediction_archived,
            "files_updated": written,
        }
        if prediction_archived:
            # Cached predictions outlive this decision; squad capture re-checks.
            result["open_until_utc"] = deadline.isoformat() if deadline else None
        if skipped_reason:
            result["prediction_skipped_reason"] = skipped_reason
        logger.info(
            "Archived official data under %s (%d files updated); prediction "
            "GW%d %s",
            season_root,
            len(written),
            prediction_gameweek,
            "archived" if prediction_archived else f"not archived: {skipped_reason}",
        )
        return result

    def capture_squad(
        self,
        predictions: pd.DataFrame,
        squad: pd.DataFrame,
    ) -> dict[str, Any]:
        """Persist the selected squad when its predictions were archived.

        The season and archive decision come from the predictions' own
        ``attrs["archive"]`` result, never from another request's state.
        """
        if not self.enabled:
            return {"status": "disabled"}
        try:
            archive = predictions.attrs.get("archive") or {}
            if archive.get("status") != "saved" or not archive.get(
                "prediction_archived"
            ):
                return {
                    "status": "skipped",
                    "reason": archive.get("prediction_skipped_reason")
                    or "prediction-not-archived",
                }
            open_until = _parse_deadline(archive.get("open_until_utc"))
            prediction_gameweek = int(predictions.attrs["gameweek"])
            season = str(archive["season"])
            target = (
                self.root_path
                / season
                / "squads"
                / f"gw_{prediction_gameweek:02d}.json"
            )
            payload = {
                "archive_schema_version": self.SCHEMA_VERSION,
                "season": season,
                "prediction_gameweek": prediction_gameweek,
                "strategy": predictions.attrs.get("inference", {}).get("strategy"),
                "source": predictions.attrs.get("source", "official-fpl"),
                "players": squad.to_dict(orient="records"),
            }
            content_digest = hashlib.sha256(self._json_bytes(payload)).hexdigest()
            with self._lock:
                # Predictions can be served from cache after the deadline
                # passes; the squad file must hold only a pre-deadline pick.
                if not self._accepts_forecasts(open_until):
                    return {"status": "skipped", "reason": "gameweek-closed"}
                if self._squad_digests.get(target) == content_digest:
                    return {"status": "unchanged", "path": str(target)}
                payload["captured_at_utc"] = self.clock().isoformat()
                updated = self._write_bytes(target, self._json_bytes(payload))
                self._squad_digests[target] = content_digest
            return {
                "status": "saved",
                "path": str(target),
                "updated": updated,
            }
        except Exception as error:  # Archive availability must not break the API.
            logger.exception("Could not persist the selected squad")
            return {"status": "failed", "error": str(error)}

    def frozen_forecast(
        self, official_client: Any, gameweek: int
    ) -> Optional[pd.DataFrame]:
        """Return a closed gameweek's archived pre-deadline forecast.

        Once a gameweek's deadline has passed, its prediction must never
        change, so the archived predictions are returned exactly as captured;
        the squad is re-selected from them, which gives the same squad.
        Returns None while the gameweek is still open, when the archive is
        disabled, or when the archive cannot prove it holds a pre-deadline
        forecast (for example, files written by an older version); callers
        then predict live and mark the result as not frozen.
        """
        if not self.enabled:
            return None
        try:
            bootstrap = official_client.bootstrap()
            deadline = self._gameweek_deadline(bootstrap, gameweek)
            if deadline is None or self.clock() < deadline:
                return None
            season_root = self.root_path / self._season_name(bootstrap)
            metadata_path = season_root / "metadata" / f"gw_{gameweek:02d}.json"
            # Forecast writes hold the same lock and replace this file in one
            # step, so it is always a complete forecast.
            with self._lock:
                if not metadata_path.is_file():
                    logger.warning(
                        "GW%d is closed but no pre-deadline forecast was "
                        "archived; predicting live",
                        gameweek,
                    )
                    return None
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            problem = self._forecast_problem(metadata, gameweek, deadline)
            if problem:
                logger.warning(
                    "GW%d is closed but its archived forecast %s; predicting live",
                    gameweek,
                    problem,
                )
                return None
            forecast = metadata["forecast"]
            predictions = pd.DataFrame(forecast["data"], columns=forecast["columns"])
        except Exception:  # Fail open: a live prediction beats an error.
            logger.exception("Could not read the archived GW%d forecast", gameweek)
            return None

        predictions.attrs.update(
            gameweek=gameweek,
            inference=metadata.get("inference", {}),
            source=metadata.get("source", "official-fpl"),
            frozen=True,
            forecast_captured_at=metadata.get("captured_at_utc"),
            archive={
                "status": "frozen",
                "prediction_archived": False,
                "prediction_skipped_reason": "gameweek-closed",
            },
        )
        return predictions

    @staticmethod
    def _forecast_problem(
        metadata: Mapping[str, Any], gameweek: int, deadline: datetime
    ) -> Optional[str]:
        """Explain why archived metadata is not a provable pre-deadline forecast."""
        if metadata.get("prediction_gameweek") != gameweek:
            return "belongs to another gameweek"
        forecast = metadata.get("forecast")
        if not isinstance(forecast, Mapping) or not {"columns", "data"} <= set(
            forecast
        ):
            return "has no stored forecast (written by an older version)"
        captured_at = _parse_deadline(metadata.get("captured_at_utc"))
        if captured_at is None or captured_at >= deadline:
            return "was not captured before the deadline"
        return None

    @staticmethod
    def _forecast_payload(predictions: pd.DataFrame) -> dict[str, Any]:
        """Predictions as exact JSON: column order, then one list per row."""
        return {
            "columns": [str(column) for column in predictions.columns],
            "data": _json_safe(predictions.to_numpy(dtype=object).tolist()),
        }

    def _capture_official_history(
        self,
        official_client: Any,
        bootstrap: Mapping[str, Any],
        season_root: Path,
        written: list[str],
        fallback_history: Optional[pd.DataFrame] = None,
    ) -> tuple[int, pd.DataFrame]:
        """Archive cumulative official history and per-gameweek player stats."""
        finished_gameweeks = [
            int(event["id"])
            for event in bootstrap.get("events", [])
            if event.get("finished")
        ]
        # Only finished gameweeks enter the cumulative history, so a live or
        # far-future request never archives partial match data. The cutoff
        # reaches 39 after GW38 so the final event is not omitted.
        history_cutoff_gameweek = max(finished_gameweeks, default=0) + 1

        # Archive removed/unavailable players too. Existing official HTTP cache
        # makes this inexpensive for the selectable players already fetched.
        try:
            history = official_client.player_history(
                history_cutoff_gameweek, selectable_only=False
            )
        except TypeError:
            history = (
                fallback_history
                if fallback_history is not None
                else official_client.player_history(history_cutoff_gameweek)
            )
        history = training_safe_history(history)
        self._record_frame(
            written,
            season_root,
            season_root
            / "official"
            / "history"
            / f"before_gw_{history_cutoff_gameweek:02d}.csv",
            history,
        )
        self._record_gameweek_frames(
            written,
            season_root,
            season_root / "official" / "player-stats",
            history,
        )
        return history_cutoff_gameweek, history

    def _accepts_forecasts(self, deadline: Optional[datetime]) -> bool:
        """Whether a forecast for a gameweek with this deadline may be written."""
        return deadline is None or self.clock() < deadline

    def _commit_forecast(
        self,
        written: list[str],
        season_root: Path,
        inputs: Mapping[Path, bytes],
        predictions_path: Path,
        predictions_csv: bytes,
        metadata_path: Path,
        metadata: Mapping[str, Any],
        deadline: Optional[datetime],
    ) -> bool:
        """Write a forecast's files while its gameweek is still open.

        The deadline is checked after the inputs were fetched, under the lock
        frozen reads take, so a capture that runs past the deadline writes
        nothing and a forecast already served as frozen never changes. The
        metadata, which holds the forecast served once the gameweek closes,
        is written last in a single atomic replace, so a failure part-way
        leaves the previous forecast intact. Returns False once the deadline
        has passed.
        """
        with self._lock:
            if not self._accepts_forecasts(deadline):
                return False
            metadata = {**metadata, "captured_at_utc": self.clock().isoformat()}
            for path, content in (
                *inputs.items(),
                (predictions_path, predictions_csv),
                (metadata_path, self._json_bytes(metadata)),
            ):
                self._record_write(written, season_root, path, content)
        return True

    def _archive_live_gameweeks(
        self,
        official_client: Any,
        bootstrap: Mapping[str, Any],
        season_root: Path,
        written: list[str],
    ) -> list[int]:
        archived: list[int] = []
        for event in bootstrap.get("events", []):
            gameweek = int(event["id"])
            finished = bool(event.get("finished"))
            current = bool(event.get("is_current"))
            if not finished and not current:
                continue
            target = season_root / "official" / "live" / f"gw_{gameweek:02d}.json"
            marker = target.with_suffix(".checked")
            checked = bool(event.get("data_checked"))
            # Points can still be corrected after "finished"; only a payload
            # fetched well after the final data check is immutable.
            if checked and target.is_file() and self._live_settled(marker):
                archived.append(gameweek)
                continue
            try:
                payload = official_client.event_live(gameweek)
            except AttributeError:
                payload = official_client.mapped_event_live(gameweek)
            self._record_write(
                written, season_root, target, self._json_bytes(payload)
            )
            if checked and not marker.is_file():
                self._write_bytes(
                    marker,
                    self._json_bytes(
                        {"data_checked_seen_at_utc": self.clock().isoformat()}
                    ),
                )
            archived.append(gameweek)
        return archived

    def _live_settled(self, marker: Path) -> bool:
        """Whether ``data_checked`` was first seen at least LIVE_SETTLE ago."""
        try:
            seen = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        seen_at = _parse_deadline(seen.get("data_checked_seen_at_utc"))
        return seen_at is not None and self.clock() >= seen_at + self.LIVE_SETTLE

    def _record_gameweek_frames(
        self,
        written: list[str],
        season_root: Path,
        directory: Path,
        frame: pd.DataFrame,
        skip_gameweeks: Optional[set[int]] = None,
    ) -> None:
        if "gameweek" not in frame.columns or frame.empty:
            return
        gameweeks = pd.to_numeric(frame["gameweek"], errors="coerce")
        for gameweek in sorted(int(value) for value in gameweeks.dropna().unique()):
            if gameweek < 1 or gameweek in (skip_gameweeks or set()):
                continue
            rows = frame.loc[gameweeks == gameweek].copy()
            self._record_frame(
                written,
                season_root,
                directory / f"gw_{gameweek:02d}.csv",
                rows,
            )

    def _record_frame(
        self,
        written: list[str],
        season_root: Path,
        path: Path,
        frame: pd.DataFrame,
    ) -> None:
        content = frame.to_csv(index=False).encode("utf-8")
        self._record_write(written, season_root, path, content)

    def _record_write(
        self,
        written: list[str],
        season_root: Path,
        path: Path,
        content: bytes,
    ) -> None:
        if self._write_bytes(path, content):
            written.append(str(path.relative_to(season_root)))

    def _write_bytes(self, path: Path, content: bytes) -> bool:
        digest = hashlib.sha256(content).hexdigest()
        with self._lock:
            if self._digests.get(path) == digest:
                return False
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
            self._digests[path] = digest
        return True

    @staticmethod
    def _gameweek_deadline(
        bootstrap: Mapping[str, Any], gameweek: int
    ) -> Optional[datetime]:
        event = next(
            (
                event
                for event in bootstrap.get("events", [])
                if int(event["id"]) == gameweek
            ),
            None,
        )
        return _parse_deadline(event.get("deadline_time")) if event else None

    def _open_gameweek(self, bootstrap: Mapping[str, Any]) -> Optional[int]:
        """Return the next gameweek whose deadline has not yet passed."""
        events = bootstrap.get("events", [])
        now = self.clock()
        deadlines = [
            (_parse_deadline(event.get("deadline_time")), int(event["id"]))
            for event in events
        ]
        upcoming = [
            (deadline, gameweek)
            for deadline, gameweek in deadlines
            if deadline is not None and deadline > now
        ]
        if upcoming:
            return min(upcoming)[1]
        if any(deadline is not None for deadline, _ in deadlines):
            return None
        next_event = next((event for event in events if event.get("is_next")), None)
        return int(next_event["id"]) if next_event else None

    def _season_name(self, bootstrap: Mapping[str, Any]) -> str:
        if self.configured_season.casefold() != "auto":
            return self.configured_season
        deadlines = [
            str(event.get("deadline_time"))
            for event in bootstrap.get("events", [])
            if event.get("deadline_time")
        ]
        if deadlines:
            start_year = int(min(deadlines)[:4])
            return f"{start_year}-{start_year + 1}"
        raise ValueError(
            "Cannot infer archive season from Official FPL; configure data_archive.season"
        )

    @staticmethod
    def _json_bytes(payload: Any) -> bytes:
        return (
            json.dumps(
                _json_safe(payload),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

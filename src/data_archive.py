"""Durable, fail-open archives for future OpenFPL model training."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime, timezone
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
    deadline), so the archive keeps the last pre-deadline forecast. Cumulative
    official history is refreshed on every capture, and live event files are
    immutable once Official FPL marks them ``data_checked``.
    """

    SCHEMA_VERSION = 2

    def __init__(
        self,
        root_path: Path,
        *,
        enabled: bool = True,
        configured_season: str = "auto",
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.root_path = Path(root_path)
        self.enabled = bool(enabled)
        self.configured_season = configured_season.strip() or "auto"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._digests: dict[Path, str] = {}
        self._squad_digests: dict[Path, str] = {}
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
        captured_at = self.clock().isoformat()
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
            complete_official_history = official_client.player_history(
                history_cutoff_gameweek, selectable_only=False
            )
        except TypeError:
            complete_official_history = official_history
        complete_official_history = training_safe_history(complete_official_history)
        enriched_history = training_safe_history(enriched_history)

        written: list[str] = []
        self._record_frame(
            written,
            season_root,
            season_root
            / "official"
            / "history"
            / f"before_gw_{history_cutoff_gameweek:02d}.csv",
            complete_official_history,
        )
        self._record_gameweek_frames(
            written,
            season_root,
            season_root / "official" / "player-stats",
            complete_official_history,
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
        prediction_archived = prediction_gameweek == open_gameweek
        skipped_reason = None
        if prediction_archived:
            snapshot_root = season_root / "official" / "snapshots" / gameweek_name
            self._record_write(
                written,
                season_root,
                snapshot_root / "bootstrap.json",
                self._json_bytes(bootstrap),
            )
            self._record_write(
                written,
                season_root,
                snapshot_root / "fixtures.json",
                self._json_bytes(official_client.fixtures()),
            )
            self._record_frame(
                written,
                season_root,
                season_root / "enriched" / "history" / f"before_{gameweek_name}.csv",
                enriched_history,
            )
            self._record_frame(
                written,
                season_root,
                season_root / "predictions" / f"{gameweek_name}.csv",
                predictions,
            )
            metadata = {
                "archive_schema_version": self.SCHEMA_VERSION,
                "captured_at_utc": captured_at,
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
            self._record_write(
                written,
                season_root,
                season_root / "metadata" / f"{gameweek_name}.json",
                self._json_bytes(metadata),
            )
        else:
            skipped_reason = (
                "no-open-gameweek"
                if open_gameweek is None
                else f"gameweek-not-open (open: {open_gameweek})"
            )

        result = {
            "status": "saved",
            "season": season,
            "prediction_gameweek": prediction_gameweek,
            "prediction_archived": prediction_archived,
            "files_updated": written,
        }
        if prediction_archived:
            deadline = self._gameweek_deadline(bootstrap, prediction_gameweek)
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
            # Predictions can be served from cache after the deadline passes;
            # the squad file must still hold only a pre-deadline selection.
            open_until = _parse_deadline(archive.get("open_until_utc"))
            if open_until is not None and self.clock() >= open_until:
                return {"status": "skipped", "reason": "gameweek-closed"}
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
            # Points can still be corrected after "finished"; only the final
            # data check makes a live payload immutable.
            if event.get("data_checked") and target.is_file():
                archived.append(gameweek)
                continue
            try:
                payload = official_client.event_live(gameweek)
            except AttributeError:
                payload = official_client.mapped_event_live(gameweek)
            self._record_write(
                written, season_root, target, self._json_bytes(payload)
            )
            archived.append(gameweek)
        return archived

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

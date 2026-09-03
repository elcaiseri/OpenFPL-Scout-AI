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
from typing import Any, Mapping, Optional

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


class DataArchive:
    """Write reproducible gameweek artifacts to a mounted data directory.

    Writes are deliberately fail-open: prediction requests continue if an
    archive bucket is temporarily unavailable. Stable gameweek paths are
    replaced with the freshest snapshot, while finalized live event files are
    treated as immutable.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        root_path: Path,
        *,
        enabled: bool = True,
        configured_season: str = "auto",
    ) -> None:
        self.root_path = Path(root_path)
        self.enabled = bool(enabled)
        self.configured_season = configured_season.strip() or "auto"
        self._lock = RLock()
        self._digests: dict[Path, str] = {}
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
        fixtures = official_client.fixtures()
        season = self._season_name(bootstrap)
        season_root = self.root_path / season
        gameweek_name = f"gw_{prediction_gameweek:02d}"
        captured_at = datetime.now(timezone.utc).isoformat()
        finished_gameweeks = [
            int(event["id"])
            for event in bootstrap.get("events", [])
            if event.get("finished")
        ]
        history_cutoff_gameweek = max(
            prediction_gameweek,
            max(finished_gameweeks, default=0) + 1,
        )

        # Archive removed/unavailable players too. Existing official HTTP cache
        # makes this inexpensive for the selectable players already fetched.
        # The cutoff reaches 39 after GW38 so the final event is not omitted.
        try:
            complete_official_history = official_client.player_history(
                history_cutoff_gameweek, selectable_only=False
            )
        except TypeError:
            complete_official_history = official_history

        written: list[str] = []
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
            self._json_bytes(fixtures),
        )
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
        self._record_frame(
            written,
            season_root,
            season_root / "enriched" / "history" / f"before_{gameweek_name}.csv",
            enriched_history,
        )
        self._record_gameweek_frames(
            written,
            season_root,
            season_root / "enriched" / "player-stats",
            enriched_history,
        )
        self._record_frame(
            written,
            season_root,
            season_root / "predictions" / f"{gameweek_name}.csv",
            predictions,
        )

        live_gameweeks = self._archive_live_gameweeks(
            official_client, bootstrap, season_root, written
        )
        metadata = {
            "archive_schema_version": self.SCHEMA_VERSION,
            "captured_at_utc": captured_at,
            "season": season,
            "prediction_gameweek": prediction_gameweek,
            "official_history_before_gameweek": history_cutoff_gameweek,
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
        result = {
            "status": "saved",
            "season": season,
            "prediction_gameweek": prediction_gameweek,
            "files_updated": written,
        }
        logger.info(
            "Archived prediction GW%d under %s (%d files updated)",
            prediction_gameweek,
            season_root,
            len(written),
        )
        return result

    def capture_squad(
        self,
        predictions: pd.DataFrame,
        squad: pd.DataFrame,
    ) -> dict[str, Any]:
        """Persist the selected squad after a successful optimization."""
        if not self.enabled:
            return {"status": "disabled"}
        try:
            prediction_gameweek = int(predictions.attrs["gameweek"])
            season = str(
                self.last_result.get("season") or self.configured_season
            )
            if season == "auto":
                raise ValueError("Season is unavailable before inference is archived")
            target = (
                self.root_path
                / season
                / "squads"
                / f"gw_{prediction_gameweek:02d}.json"
            )
            payload = {
                "archive_schema_version": self.SCHEMA_VERSION,
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "season": season,
                "prediction_gameweek": prediction_gameweek,
                "strategy": predictions.attrs.get("inference", {}).get("strategy"),
                "source": predictions.attrs.get("source", "official-fpl"),
                "players": squad.to_dict(orient="records"),
            }
            updated = self._write_bytes(target, self._json_bytes(payload))
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
            if finished and target.is_file():
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
    ) -> None:
        if "gameweek" not in frame.columns or frame.empty:
            return
        gameweeks = pd.to_numeric(frame["gameweek"], errors="coerce")
        for gameweek in sorted(int(value) for value in gameweeks.dropna().unique()):
            if gameweek < 1:
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

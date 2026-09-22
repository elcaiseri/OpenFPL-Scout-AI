"""Bounded owner capture records, stored separately from forecast evidence."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4


class CaptureHistory:
    """One atomic file per attempt avoids conflicts between server workers.

    Persistence failures remain visible but never prevent forecast capture.
    Records contain operation diagnostics, never credentials or manager IDs.
    """

    def __init__(self, root: Path, limit=100):
        self.root = root / "operations" / "captures"
        self.limit = limit
        self.lock = RLock()
        self.recent = {}
        self.warning = None

    def start(self, gameweek):
        now = datetime.now(timezone.utc)
        record = {
            "id": f"{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid4().hex}",
            "gameweek": gameweek, "season": None,
            "started_at_utc": now.isoformat(), "finished_at_utc": None,
            "duration_seconds": None, "status": "running", "phase": "deadline check",
            "archive_status": "not-attempted", "squad_status": "not-attempted",
            "successful_models": [], "failed_models": [], "error": None,
        }
        self.save(record)
        return record

    def save(self, record):
        with self.lock:
            self.recent[record["id"]] = dict(record)
            temporary = None
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(record, handle, allow_nan=False)
                os.replace(temporary, self.root / f"{record['id']}.json")
                self.warning = None
                # Never remove an unfinished attempt while it might be running.
                paths = sorted(self.root.glob("*.json"), reverse=True)
                for path in paths[self.limit:]:
                    try:
                        if json.loads(path.read_text()).get("status") != "running":
                            path.unlink(missing_ok=True)
                    except (OSError, ValueError, AttributeError):
                        continue
            except OSError:
                self.warning = "Capture history could not be saved. Recent attempts are available in this process only; check archive write permissions."
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                self.recent = dict(sorted(self.recent.items(), reverse=True)[:self.limit])

    def snapshot(self):
        with self.lock:
            records = {}
            warning = self.warning
            try:
                for path in sorted(self.root.glob("*.json"), reverse=True)[:self.limit]:
                    try:
                        record = json.loads(path.read_text())
                        if not isinstance(record, dict) or record.get("id") != path.stem:
                            raise ValueError("Invalid capture record")
                        records[record["id"]] = record
                    except (OSError, ValueError):
                        warning = warning or "Some capture history records could not be read."
            except OSError:
                warning = warning or "Saved capture history is unavailable."
            records.update(self.recent)
            return {
                "attempts": [record for _, record in sorted(records.items(), reverse=True)[:self.limit]],
                "warning": warning,
                "limit": self.limit,
                "scope": "Latest owner-requested captures across all seasons. Automatic predictions and transfer planning are excluded. A running record has no recorded outcome yet, including after an interrupted process.",
            }

"""Owner monitoring and timestamp-verified forecast evaluation.

Reading this dashboard never runs inference. Official event-live scores are
already gameweek totals (including double gameweeks); join them by player ID.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

import pandas as pd

from src.data_archive import _json_safe, utc_datetime

SEASON_PATTERN = re.compile(r"^\d{4}-\d{4}$")
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def number(value: Any):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def metrics(players: list[dict]) -> dict:
    matched = [p for p in players if p["actual_points"] is not None]
    errors = [p["expected_points"] - p["actual_points"] for p in matched]
    count = len(errors)
    return {
        "count": count,
        "mae": sum(abs(e) for e in errors) / count if count else None,
        "rmse": math.sqrt(sum(e * e for e in errors) / count) if count else None,
        "bias": sum(errors) / count if count else None,
        "within_two_pct": 100 * sum(abs(e) <= 2 for e in errors) / count if count else None,
    }


class RuntimeMonitor:
    """Bounded telemetry for this process only; never retains URLs or credentials."""

    def __init__(self):
        self.started = time.monotonic()
        self.requests = 0
        self.server_errors = 0
        self.recent = deque(maxlen=200)
        self.lock = RLock()

    def record(self, status: int, duration: float):
        with self.lock:
            self.requests += 1
            self.server_errors += status >= 500
            self.recent.append(duration * 1000)

    def snapshot(self):
        with self.lock:
            durations = sorted(self.recent)
            return {
                "uptime_seconds": time.monotonic() - self.started,
                "requests": self.requests,
                "server_errors": self.server_errors,
                "latency_sample_size": len(durations),
                "p95_latency_ms": durations[max(0, math.ceil(len(durations) * .95) - 1)] if durations else None,
                "scope": "This process since startup; latency uses the latest 200 non-dashboard requests.",
            }


class AdminDashboard:
    def __init__(self, scout, *, cache_seconds=60):
        self.scout = scout
        self.root = scout.data_archive.root_path
        self.cache_seconds = cache_seconds
        self.cache = {}
        self.lock = RLock()

    def report(self, season=None, gameweek=None):
        if season is not None and not SEASON_PATTERN.fullmatch(season):
            raise ValueError("Invalid season")
        with self.lock:
            cached = self.cache.get(season)
            if not cached or time.monotonic() - cached[0] >= self.cache_seconds:
                report = self._build(season)
                self.cache[season] = (time.monotonic(), report)
            else:
                report = cached[1]
        weeks = report["gameweeks"]
        selected = next((w for w in weeks if w["gameweek"] == gameweek), None)
        if gameweek is not None and selected is None:
            raise ValueError("Gameweek is unavailable in this season")
        if selected is None:
            predicted = [w for w in weeks if w["prediction_count"]]
            scored = [w for w in predicted if w["metrics"]["count"]]
            selected = (scored or predicted or [w for w in weeks if w["is_current"]] or weeks or [None])[-1]
        return _json_safe({
            **report,
            "gameweeks": [{k: v for k, v in w.items() if k not in {"players", "metadata", "squad"}} for w in weeks],
            "selected": selected,
            "system": self._system(report),
        })

    def _system(self, report):
        metadata = report.get("latest_metadata", {})
        inference = metadata.get("inference", {})
        successful = inference.get("successful_models", [])
        failed = inference.get("failed_models", {})
        loaded = {m.name for m in self.scout.model_artifacts}
        models = []
        for name, model in self.scout.config.get("models", {}).items():
            models.append({
                "name": name, "loaded": name in loaded,
                "version": model.get("version"), "last_trained": model.get("last_trained"),
                "last_inference": "failed" if name in failed else "succeeded" if name in successful else "not-recorded",
                "error": failed.get(name),
                "weight": inference.get("weights", {}).get(name),
            })
        return {
            "models": models,
            "archive": self.scout.data_archive.status(),
            "enrichment": metadata.get("enrichment", {}),
            "enrichment_enabled": self.scout.fpl_data_enabled,
            "feature_coverage": inference.get("feature_coverage"),
            "last_inference_at": metadata.get("captured_at_utc"),
            "last_inference_gameweek": metadata.get("prediction_gameweek"),
            "strategy": inference.get("strategy"),
            "mean_model_spread": inference.get("mean_model_spread"),
        }

    def _build(self, requested_season):
        warnings = []
        seasons = sorted(p.name for p in self.root.iterdir() if p.is_dir() and SEASON_PATTERN.fullmatch(p.name)) if self.root.is_dir() else []
        official = None
        current_season = None
        try:
            official = self.scout.official_client.bootstrap()
            deadlines = [str(e["deadline_time"]) for e in official.get("events", []) if e.get("deadline_time")]
            if deadlines:
                year = int(min(deadlines)[:4])
                current_season = f"{year}-{year + 1}"
                seasons = sorted(set(seasons + [current_season]))
        except Exception:
            warnings.append("Official FPL is unavailable. Showing saved data with its original timestamps.")
        season = requested_season or current_season or (seasons[-1] if seasons else None)
        if requested_season and requested_season not in seasons:
            raise ValueError("Season is unavailable")
        season_root = self.root / season if season else None
        bootstrap = official if season == current_season else None
        if bootstrap is None and season_root:
            snapshots = sorted((season_root / "official/snapshots").glob("gw_*/bootstrap.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in snapshots:
                try:
                    bootstrap = read_json(path)
                    break
                except (OSError, ValueError):
                    warnings.append("An official bootstrap snapshot could not be read.")
        events = {int(e["id"]): e for e in (bootstrap or {}).get("events", [])}
        forecasts = {}
        latest_metadata = {}
        if season_root:
            names = {p.stem for folder, suffix in [("predictions", "csv"), ("evaluation", "json")] for p in (season_root / folder).glob(f"gw_*.{suffix}") if re.fullmatch(r"gw_\d{2}", p.stem)}
            for name in sorted(names):
                gw = int(name[3:])
                if not 1 <= gw <= 38:
                    continue
                try:
                    bundle_path = season_root / "evaluation" / f"{name}.json"
                    metadata_path = season_root / "metadata" / f"{name}.json"
                    latest = read_json(metadata_path) if metadata_path.is_file() else {}
                    if (latest.get("captured_at_utc") or "") > (latest_metadata.get("captured_at_utc") or ""):
                        latest_metadata = latest
                    if bundle_path.is_file():
                        bundle = read_json(bundle_path)
                        forecasts[gw] = {**bundle, "preserved": True}
                        if (bundle.get("metadata", {}).get("captured_at_utc") or "") > (latest_metadata.get("captured_at_utc") or ""):
                            latest_metadata = bundle["metadata"]
                    else:
                        squad_path = season_root / "squads" / f"{name}.json"
                        forecasts[gw] = {
                            "metadata": latest,
                            "predictions": pd.read_csv(season_root / "predictions" / f"{name}.csv").to_dict("records"),
                            "squad": read_json(squad_path) if squad_path.is_file() else None,
                            "deadline_time": events.get(gw, {}).get("deadline_time"),
                            "preserved": False,
                        }
                except (OSError, ValueError, KeyError):
                    warnings.append(f"GW{gw}: forecast archive is unreadable; excluded from evaluation.")

        now = datetime.now(timezone.utc)
        # Only fetch event totals for archived forecasts whose deadlines passed.
        to_fetch = [gw for gw in forecasts if season == current_season and utc_datetime(events.get(gw, {}).get("deadline_time")) and utc_datetime(events[gw]["deadline_time"]) <= now]

        def actuals(gw):
            path = season_root / "evaluation/actuals" / f"gw_{gw:02d}.json"
            saved = None
            try:
                saved = read_json(path) if path.is_file() else None
                if saved and (saved.get("season") != season or saved.get("gameweek") != gw):
                    saved = None
                if saved and saved.get("finalized"):
                    return gw, saved, None
            except (OSError, ValueError):
                pass
            try:
                event = events[gw]
                finalized = bool(event.get("finished") and event.get("data_checked"))
                # A live payload cached before bonus checking must not become
                # an immutable final result merely because bootstrap advanced.
                payload = self.scout.official_client.event_live(gw, refresh=finalized)
                if not isinstance(payload.get("elements"), list) or not payload["elements"]:
                    raise ValueError("Missing official player scores")
                result = {
                    "season": season, "gameweek": gw,
                    "payload": payload, "fetched_at_utc": now.isoformat(),
                    "finalized": finalized,
                    "source": "official-fpl",
                }
                if self.scout.data_archive.enabled:
                    try:
                        self.scout.data_archive._write_bytes(path, self.scout.data_archive._json_bytes(result))
                    except OSError:
                        return gw, result, f"GW{gw}: official scores loaded, but could not be saved."
                return gw, result, None
            except Exception:
                return gw, saved, f"GW{gw}: official scores could not be refreshed. Saved scores may be provisional."

        results = {}
        if to_fetch:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for gw, result, warning in pool.map(actuals, to_fetch):
                    results[gw] = result
                    if warning:
                        warnings.append(warning)
        weeks = []
        for gw in sorted(set(events) | set(forecasts)):
            result = results.get(gw)
            if result is None and season_root:
                for path, legacy in [(season_root / "evaluation/actuals" / f"gw_{gw:02d}.json", False), (season_root / "official/live" / f"gw_{gw:02d}.json", True)]:
                    try:
                        if path.is_file():
                            saved = read_json(path)
                            if not legacy and (saved.get("season") != season or saved.get("gameweek") != gw):
                                continue
                            result = {"payload": saved, "finalized": False, "fetched_at_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), "source": "legacy-archive"} if legacy else saved
                            break
                    except (OSError, ValueError):
                        warnings.append(f"GW{gw}: saved official results are unreadable.")
            try:
                weeks.append(self._week(gw, season, events.get(gw, {}), forecasts.get(gw), result))
            except (ValueError, KeyError, TypeError):
                warnings.append(f"GW{gw}: invalid or duplicate player data; excluded from evaluation.")
                weeks.append(self._week(gw, season, events.get(gw, {}), None, None))
        evaluated = [w for w in weeks if w["eligible"] and w["result_state"] == "final"]
        all_players = [p for w in evaluated for p in w["players"]]
        compared = [w for w in weeks if w["result_state"] == "final" and w["metrics"]["count"]]
        comparison_summary = {
            **metrics([p for w in compared for p in w["players"]]),
            "evaluated_gameweeks": len(compared),
            "archived_gameweeks": len(forecasts),
            "verified_gameweeks": sum(w["eligible"] for w in compared),
            "post_deadline_gameweeks": sum(w["forecast_state"] == "post-deadline" for w in compared),
            "unknown_timing_gameweeks": sum(w["forecast_state"] == "unverified" for w in compared),
        }
        return {
            "generated_at_utc": now.isoformat(), "season": season, "seasons": seasons,
            "official_status": "available" if official is not None else "unavailable",
            "gameweeks": weeks, "latest_metadata": latest_metadata,
            "summary": {**metrics(all_players), "evaluated_gameweeks": len([w for w in evaluated if w["metrics"]["count"]]), "archived_gameweeks": len(forecasts)},
            "comparison_summary": comparison_summary,
            "warnings": warnings,
        }

    @staticmethod
    def _week(gw, season, event, forecast, actual):
        forecast = forecast or {}
        metadata = forecast.get("metadata", {})
        if (
            metadata.get("season", season) != season
            or metadata.get("prediction_gameweek", gw) != gw
        ):
            raise ValueError("Forecast metadata does not match its season and gameweek")
        deadline = utc_datetime(event.get("deadline_time") or forecast.get("deadline_time"))
        captured = utc_datetime(metadata.get("captured_at_utc"))
        eligible = bool(captured and deadline and captured < deadline and metadata.get("season") == season and metadata.get("prediction_gameweek") == gw)
        state = "final" if actual and actual.get("finalized") else "provisional" if actual else "awaiting-results"
        actual_by_id = {}
        for item in (actual or {}).get("payload", {}).get("elements", []):
            player_id = int(item.get("id", item.get("element_id")))
            if player_id in actual_by_id:
                raise ValueError("Duplicate official player ID")
            actual_by_id[player_id] = item.get("stats", {})
        squad = forecast.get("squad") or {}
        squad_captured = utc_datetime(squad.get("captured_at_utc"))
        squad_valid = bool(eligible and squad_captured and squad_captured >= captured and squad_captured < deadline and squad.get("season") == season and squad.get("prediction_gameweek") == gw)
        squad_by_id = {int(p["id"]): p for p in squad.get("players", [])}
        players = []
        seen = set()
        for row in forecast.get("predictions", []):
            raw_id = number(row.get("id"))
            if raw_id is None or raw_id <= 0 or not raw_id.is_integer():
                raise ValueError("Invalid player ID")
            player_id = int(raw_id)
            predicted = number(row.get("expected_points"))
            if player_id in seen or predicted is None or number(row.get("gameweek")) != gw:
                raise ValueError("Invalid forecast")
            seen.add(player_id)
            stats = actual_by_id.get(player_id, {})
            points = number(stats.get("total_points"))
            selected = squad_by_id.get(player_id)
            position = row.get("element_type")
            players.append({
                "id": player_id, "name": row["web_name"] if isinstance(row.get("web_name"), str) else str(player_id),
                "team": row["team_name"] if isinstance(row.get("team_name"), str) else "—",
                "position": POSITIONS.get(number(position), position if isinstance(position, str) else "—"),
                "expected_points": predicted, "actual_points": points,
                "error": predicted - points if points is not None else None,
                "minutes": number(stats.get("minutes")),
                "in_squad": selected is not None,
                "role": selected.get("role", "") if selected else "",
            })
        players.sort(key=lambda p: p["expected_points"], reverse=True)
        selected_players = [p for p in players if p["in_squad"]]
        # Reject a separately overwritten squad that no longer matches its forecast.
        squad_valid = squad_valid and bool(selected_players) and len(selected_players) == len(squad_by_id) and all(number(squad_by_id[p["id"]].get("expected_points")) == p["expected_points"] for p in selected_players)
        captain = next((p for p in selected_players if p["role"] == "captain"), None)
        complete = bool(selected_players) and all(p["actual_points"] is not None for p in selected_players)
        return {
            "gameweek": gw, "is_current": bool(event.get("is_current")),
            "deadline_time": deadline.isoformat() if deadline else None,
            "captured_at_utc": metadata.get("captured_at_utc"),
            "actuals_at_utc": (actual or {}).get("fetched_at_utc"),
            "actuals_source": (actual or {}).get("source"),
            "prediction_count": len(players), "eligible": eligible and bool(players),
            "forecast_state": (
                "missing" if not players else "pre-deadline" if eligible
                else "post-deadline" if captured and deadline and captured >= deadline
                else "unverified"
            ),
            "preserved": forecast.get("preserved", False), "result_state": state,
            "metrics": metrics(players), "players": players, "metadata": metadata,
            "positions": [{"position": position, **metrics([p for p in players if p["position"] == position])} for position in POSITIONS.values()],
            "squad": {
                "eligible": bool(squad_valid), "count": len(selected_players),
                "expected_points": sum(p["expected_points"] for p in selected_players) if selected_players else None,
                "actual_points": sum(p["actual_points"] for p in selected_players) if complete else None,
                "matched": sum(p["actual_points"] is not None for p in selected_players),
                "captain": captain,
                "note": "All selected players, counted once. This is a 15-player shortlist, without bench substitutions or captain multipliers.",
            },
        }

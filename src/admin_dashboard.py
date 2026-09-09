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
from src.observatory import actual_summary, gameweek_analysis, point_metrics, season_analysis
from src.manager_lab import (
    catalog as manager_catalog,
    estimate_free_transfers,
    optimize as optimize_transfers,
    review as manager_review,
    squad_from_picks,
)
from src.model_lab import ModelLab
from src.scout_replay import load_replay

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
    return point_metrics(players)


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
        self.model_lab = ModelLab(scout.config)

    def _season_report(self, season=None):
        if season is not None and not SEASON_PATTERN.fullmatch(season):
            raise ValueError("Invalid season")
        with self.lock:
            cached = self.cache.get(season)
            if not cached or time.monotonic() - cached[0] >= self.cache_seconds:
                report = self._build(season)
                self.cache[season] = (time.monotonic(), report)
            else:
                report = cached[1]
        return report

    def report(self, season=None, gameweek=None):
        report = self._season_report(season)
        weeks = report["gameweeks"]
        selected = next((w for w in weeks if w["gameweek"] == gameweek), None)
        if gameweek is not None and selected is None:
            raise ValueError("Gameweek is unavailable in this season")
        if selected is None:
            predicted = [w for w in weeks if w["prediction_count"]]
            scored = [w for w in predicted if any(p["actual_points"] is not None for p in w["players"])]
            selected = (scored or predicted or [w for w in weeks if w["is_current"]] or weeks or [None])[-1]
        return _json_safe({
            **report,
            "gameweeks": [{k: v for k, v in w.items() if k not in {"players", "metadata", "squad", "official_stats", "analysis"}} for w in weeks],
            "selected": {k: v for k, v in selected.items() if k != "official_stats"} if selected else None,
            "system": self._system(report),
        })

    def player_report(self, player_id, season=None):
        report = self._season_report(season)
        history = []
        for week in report["gameweeks"]:
            player = next((p for p in week["players"] if p["id"] == player_id), None)
            if player:
                history.append({**player, "gameweek": week["gameweek"], "forecast_state": week["forecast_state"], "result_state": week["result_state"], "captured_at_utc": week["captured_at_utc"]})
        if not history:
            raise ValueError("This player has no archived forecasts in the selected season")
        finalized = [p for p in history if p["result_state"] == "final"]
        return _json_safe({"season": report["season"], "player": history[-1], "history": history, "metrics": metrics(finalized), "actual": actual_summary(finalized)})

    def _entry_gameweeks(self, history):
        played = sorted({int(n) for n in (number(r.get("event")) for r in history.get("current") or []) if n})
        if not played:
            raise ValueError("This entry has no completed gameweeks")
        return played

    def manager_review(self, entry_id, season=None, gameweek=None):
        """Join a public FPL entry with our archived forecasts. Runs no inference."""
        report = self._season_report(season)
        client = self.scout.official_client
        entry = client.mapped_manager(entry_id)
        history = client.manager_history(entry_id)
        played = self._entry_gameweeks(history)
        selected = gameweek if gameweek in played else played[-1]
        weeks = {w["gameweek"]: w for w in report["gameweeks"]}
        detail = manager_review(client.mapped_manager_picks(entry_id, selected), weeks.get(selected), entry)
        timeline = []
        for row in history.get("current") or []:
            gw = number(row.get("event"))
            if gw is None:
                continue
            analysis = (weeks.get(int(gw)) or {}).get("analysis") or {}
            squad = analysis.get("squad") or {}
            bank, value = number(row.get("bank")), number(row.get("value"))
            timeline.append({
                "gameweek": int(gw),
                "official_points": number(row.get("points")),
                "bench_points": number(row.get("points_on_bench")),
                "transfers": number(row.get("event_transfers")),
                "transfers_cost": number(row.get("event_transfers_cost")),
                "overall_rank": number(row.get("overall_rank")),
                "bank": bank / 10 if bank is not None else None,
                "squad_value": value / 10 if value is not None else None,
                "our_predicted_points": squad.get("predicted_points"),
                "our_actual_points": squad.get("actual_points"),
                "official_average": analysis.get("average_manager_score"),
            })
        scored = [t for t in timeline if t["official_points"] is not None and t["our_actual_points"] is not None]
        return _json_safe({
            "season": report["season"],
            "entry": {
                "id": entry.get("id"), "name": entry.get("name"),
                "manager": " ".join(filter(None, [entry.get("player_first_name"), entry.get("player_last_name")])) or None,
                "overall_points": entry.get("summary_overall_points"),
                "overall_rank": entry.get("summary_overall_rank"),
                "current_event": entry.get("current_event"),
                "started_event": entry.get("started_event"),
                "squad_value": entry.get("last_deadline_value"),
                "bank": entry.get("last_deadline_bank"),
            },
            "gameweek": selected, "gameweeks": played,
            "review": detail, "timeline": timeline,
            "comparison": {
                "gameweeks": len(scored),
                "entry_points": sum(t["official_points"] for t in scored) if scored else None,
                "our_points": sum(t["our_actual_points"] for t in scored) if scored else None,
                "official_average": sum(t["official_average"] for t in scored if t["official_average"] is not None) if scored else None,
            },
            "free_transfers": estimate_free_transfers(history, (played[-1] + 1)),
            "chips": history.get("chips", []),
            "note": (
                "This entry's official points, ranks and bench totals are reported by FPL. "
                "Our comparison covers only gameweeks where both an archived forecast and a "
                "final official result exist."
            ),
        })

    def manager_plan(self, entry_id, gameweek, *, free_transfers=None, bank=None, max_transfers=3):
        """Plan transfers for an upcoming gameweek. Runs inference when no forecast is archived."""
        client = self.scout.official_client
        bootstrap = client.bootstrap()
        history = client.manager_history(entry_id)
        played = self._entry_gameweeks(history)
        source_gameweek = max([gw for gw in played if gw < gameweek] or played)
        picks = client.mapped_manager_picks(entry_id, source_gameweek)
        report = self._season_report(None)
        week = next((w for w in report["gameweeks"] if w["gameweek"] == gameweek), None)
        if week and week["prediction_count"] and week.get("is_points_forecast"):
            rows = week["players"]
            source = {"kind": "archived-forecast", "captured_at_utc": week["captured_at_utc"], "gameweek": gameweek}
        else:
            frame = self.scout.get_official_predictions(gameweek)
            rows = frame.to_dict("records")
            source = {
                "kind": "inference",
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "gameweek": gameweek,
                "strategy": frame.attrs.get("inference", {}).get("strategy"),
            }
            with self.lock:
                self.cache.clear()
        players = manager_catalog(rows, bootstrap)
        squad, unforecast, unresolved = squad_from_picks(picks, players, bootstrap)
        entry_history = picks.get("entry_history", {}) or {}
        official_bank = number(entry_history.get("bank"))
        funds = number(bank) if bank is not None else (official_bank / 10 if official_bank is not None else 0.0)
        estimate = estimate_free_transfers(history, gameweek)
        transfers = int(free_transfers) if free_transfers is not None else estimate["free_transfers"]
        pool = [p for p in players.values() if p["id"] not in {s["id"] for s in squad}]
        result = optimize_transfers(
            squad, pool, bank=funds, free_transfers=transfers, max_transfers=max_transfers
        )
        warnings = []
        if unforecast:
            warnings.append("No forecast for " + ", ".join(sorted(unforecast)) + "; they are held but never fielded in a planned XI.")
        if unresolved:
            warnings.append(f"{len(unresolved)} pick(s) are not in the official player list and were dropped; the plan is incomplete.")
        if bank is not None and official_bank is not None and abs(funds - official_bank / 10) > 1e-9:
            warnings.append("Bank was overridden; affordability uses your figure, not the official one.")
        if free_transfers is not None and transfers != estimate["free_transfers"]:
            warnings.append("Free transfers were overridden; hit costs use your figure.")
        return _json_safe({
            **result,
            "entry_id": entry_id,
            "gameweek": gameweek,
            "squad_gameweek": source_gameweek,
            "forecast_source": source,
            "bank_source": "owner-override" if bank is not None else "official-entry-history",
            "free_transfer_estimate": estimate,
            "free_transfer_source": "owner-override" if free_transfers is not None else estimate["basis"],
            "warnings": warnings,
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
        fixtures = []
        if season == current_season and hasattr(self.scout.official_client, "fixtures"):
            try:
                fixtures = self.scout.official_client.fixtures()
            except Exception:
                warnings.append("Fixture refresh failed; saved fixtures are shown when available.")
        if not fixtures and season_root:
            for path in sorted((season_root / "official/snapshots").glob("gw_*/fixtures.json"), reverse=True):
                try:
                    fixtures = json.loads(path.read_text())
                    break
                except (OSError, ValueError):
                    continue
        teams = {t["id"]: t.get("name", str(t["id"])) for t in (bootstrap or {}).get("teams", [])}
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
                            # Preserve exact archived floats when matching the JSON shortlist.
                            "predictions": pd.read_csv(season_root / "predictions" / f"{name}.csv", float_precision="round_trip").to_dict("records"),
                            "squad": read_json(squad_path) if squad_path.is_file() else None,
                            "deadline_time": events.get(gw, {}).get("deadline_time"),
                            "preserved": False,
                        }
                        diagnostics_path = season_root / "diagnostics" / f"{name}.json"
                        if diagnostics_path.is_file():
                            diagnostics = read_json(diagnostics_path)
                            if diagnostics.get("metadata", {}).get("captured_at_utc") == latest.get("captured_at_utc"):
                                forecasts[gw].update({k: diagnostics.get(k, {}) for k in ("model_predictions", "player_context")})
                    replay_path = season_root / "evaluation/replays" / f"{name}.json"
                    if bundle_path.is_file() and replay_path.is_file() and forecasts[gw]["metadata"].get("inference", {}).get("strategy") == "ownership-cold-start":
                        try:
                            forecasts[gw]["replay"] = load_replay(replay_path, bundle_path.read_bytes(), season, gw)
                        except (OSError, ValueError, KeyError, TypeError):
                            warnings.append(f"GW{gw}: retrospective model estimates do not match the saved snapshot; original evidence is shown.")
                except (OSError, ValueError, KeyError):
                    warnings.append(f"GW{gw}: forecast archive is unreadable; excluded from evaluation.")

        now = datetime.now(timezone.utc)
        # Football results remain useful even when we did not save a forecast.
        to_fetch = [gw for gw in events if season == current_season and utc_datetime(events[gw].get("deadline_time")) and utc_datetime(events[gw]["deadline_time"]) <= now]

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
            # A retrospective snapshot can retain official results independently
            # of the live cache, without changing the original forecast timing.
            snapshot_actuals = (forecasts.get(gw) or {}).get("actuals_snapshot")
            if (
                (not result or not result.get("finalized"))
                and isinstance(snapshot_actuals, dict)
                and snapshot_actuals.get("season") == season
                and snapshot_actuals.get("gameweek") == gw
                and snapshot_actuals.get("finalized")
                and utc_datetime(snapshot_actuals.get("fetched_at_utc"))
            ):
                result = snapshot_actuals
            try:
                weeks.append(self._week(gw, season, events.get(gw, {}), forecasts.get(gw), result))
            except (ValueError, KeyError, TypeError):
                warnings.append(f"GW{gw}: invalid or duplicate player data; excluded from evaluation.")
                weeks.append(self._week(gw, season, events.get(gw, {}), None, None))
            weeks[-1]["analysis"] = gameweek_analysis(weeks[-1], events.get(gw, {}), fixtures, teams)
        cold_starts = [w["gameweek"] for w in weeks if w["prediction_count"] and not w["is_points_forecast"]]
        if cold_starts:
            warnings.append("GW " + ", ".join(map(str, cold_starts)) + ": ownership selection scores are assessed by ranking and realized squad returns, not by points-error metrics.")
        for week in weeks:
            if week.get("replay"):
                warnings.append(f"GW{week['gameweek']}: {week['replay']['note']}")
        evaluated = [w for w in weeks if w["eligible"] and w["result_state"] == "final"]
        all_players = [p for w in evaluated for p in w["players"]]
        compared = [w for w in weeks if w["result_state"] == "final" and w["metrics"]["count"]]
        comparison_summary = {
            **metrics([p for w in compared for p in w["players"]]),
            "evaluated_gameweeks": len(compared),
            "archived_gameweeks": len(forecasts),
            "verified_gameweeks": sum(w["eligible"] for w in compared),
            "post_deadline_gameweeks": sum(w["forecast_state"] in {"post-deadline", "retrospective-model"} for w in compared),
            "unknown_timing_gameweeks": sum(w["forecast_state"] == "unverified" for w in compared),
        }
        return {
            "generated_at_utc": now.isoformat(), "season": season, "seasons": seasons,
            "official_status": "available" if official is not None else "unavailable",
            "gameweeks": weeks, "latest_metadata": latest_metadata,
            "summary": {**metrics(all_players), "evaluated_gameweeks": len([w for w in evaluated if w["metrics"]["count"]]), "archived_gameweeks": len(forecasts)},
            "comparison_summary": comparison_summary,
            "analytics": {"all": season_analysis(weeks), "verified": season_analysis(weeks, verified=True)},
            "warnings": warnings,
        }

    @staticmethod
    def _week(gw, season, event, forecast, actual):
        forecast = forecast or {}
        metadata = forecast.get("metadata", {})
        replay = forecast.get("replay")
        if (
            metadata.get("season", season) != season
            or metadata.get("prediction_gameweek", gw) != gw
        ):
            raise ValueError("Forecast metadata does not match its season and gameweek")
        deadline = utc_datetime(event.get("deadline_time") or forecast.get("deadline_time"))
        captured_at = replay["generated_at_utc"] if replay else metadata.get("captured_at_utc")
        captured = utc_datetime(captured_at)
        is_points_forecast = bool(replay) or metadata.get("inference", {}).get("strategy") != "ownership-cold-start"
        eligible = bool(not replay and captured and deadline and captured < deadline and metadata.get("season") == season and metadata.get("prediction_gameweek") == gw)
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
            selection_score = number(row.get("expected_points"))
            predicted = number(replay["predictions"].get(str(player_id))) if replay else selection_score if is_points_forecast else None
            if player_id in seen or selection_score is None or number(row.get("gameweek")) != gw:
                raise ValueError("Invalid forecast")
            seen.add(player_id)
            stats = actual_by_id.get(player_id, {})
            points = number(stats.get("total_points"))
            selected = squad_by_id.get(player_id)
            position = row.get("element_type")
            context = forecast.get("player_context", {}).get(str(player_id), {})
            players.append({
                "id": player_id, "name": row["web_name"] if isinstance(row.get("web_name"), str) else str(player_id),
                "team": row["team_name"] if isinstance(row.get("team_name"), str) else "—",
                "position": POSITIONS.get(number(position), position if isinstance(position, str) else "—"),
                "expected_points": predicted, "actual_points": points,
                "selection_score": selection_score,
                "error": predicted - points if points is not None and predicted is not None else None,
                "minutes": number(stats.get("minutes")),
                "goals": number(stats.get("goals_scored")), "assists": number(stats.get("assists")),
                "bonus": number(stats.get("bonus")), "clean_sheets": number(stats.get("clean_sheets")),
                "xg": number(stats.get("expected_goals")), "xa": number(stats.get("expected_assists")),
                "price": number(context.get("now_cost")) / 10 if number(context.get("now_cost")) is not None else None,
                "ownership": number(context.get("selected_by_percent", row.get("selected_by_percent"))),
                "availability": context.get("status", row.get("status")),
                "opponent": row.get("opponent_team_name"),
                "model_predictions": {name: number(values.get(str(player_id))) for name, values in (replay or forecast).get("model_predictions", {}).items()},
                "in_squad": selected is not None,
                "role": selected.get("role", "") if selected else "",
            })
        players.sort(key=lambda p: p["selection_score"], reverse=True)
        selected_players = [p for p in players if p["in_squad"]]
        # Reject a separately overwritten squad that no longer matches its forecast.
        squad_matches = bool(selected_players) and len(selected_players) == len(squad_by_id) == len(squad.get("players", [])) and all(number(squad_by_id[p["id"]].get("expected_points")) == p["selection_score"] for p in selected_players) and squad.get("season") == season and squad.get("prediction_gameweek") == gw
        squad_valid = squad_valid and squad_matches
        captain = next((p for p in selected_players if p["role"] == "captain"), None)
        complete = bool(selected_players) and all(p["actual_points"] is not None for p in selected_players)
        return {
            "gameweek": gw, "is_current": bool(event.get("is_current")),
            "deadline_time": deadline.isoformat() if deadline else None,
            "captured_at_utc": captured_at,
            "selection_captured_at_utc": metadata.get("captured_at_utc"),
            "replay": {k: replay[k] for k in ("generated_at_utc", "note")} if replay else None,
            "actuals_at_utc": (actual or {}).get("fetched_at_utc"),
            "actuals_source": (actual or {}).get("source"),
            "prediction_count": len(players), "eligible": eligible and bool(players),
            "matched_actuals": sum(p["actual_points"] is not None for p in players),
            "official_player_count": len(actual_by_id),
            "forecast_state": (
                "missing" if not players else "retrospective-model" if replay else "pre-deadline" if eligible
                else "post-deadline" if captured and deadline and captured >= deadline
                else "unverified"
            ),
            "preserved": forecast.get("preserved", False), "result_state": state,
            "snapshot_kind": forecast.get("snapshot_kind"),
            "snapshot_created_at_utc": forecast.get("snapshot_created_at_utc"),
            "is_points_forecast": is_points_forecast,
            "official_stats": actual_by_id,
            "metrics": metrics(players), "players": players, "metadata": metadata,
            "positions": [{"position": position, **metrics([p for p in players if p["position"] == position]), "actual": actual_summary([p for p in players if p["position"] == position])} for position in POSITIONS.values()],
            "squad": {
                "eligible": bool(squad_valid), "count": len(selected_players),
                "captured_at_utc": squad.get("captured_at_utc"),
                "matches_forecast": squad_matches,
                "expected_points": sum(p["expected_points"] for p in selected_players) if selected_players and is_points_forecast else None,
                "actual_points": sum(p["actual_points"] for p in selected_players) if complete else None,
                "matched": sum(p["actual_points"] is not None for p in selected_players),
                "captain": captain,
                "note": "All selected players, counted once. This is a 15-player shortlist, without bench substitutions or captain multipliers.",
            },
        }

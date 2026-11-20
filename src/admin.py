"""Private owner-dashboard API: observability, explanations and control.

Every route needs a bearer token from ``VALID_API_KEYS`` and is hidden from
the public OpenAPI schema and the ``/api`` catalog. ``OPENFPL_ADMIN_ENABLED``
set to false answers every route (and the ``/admin`` page) with 404.

Read routes never change a forecast. Live controls change the running
service until it restarts and are written to the decision log; the
``/simulate`` sandbox changes nothing.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src import insights, my_team, optimizer
from src import auth as auth_module
from src.auth import verify_api_key
from src.features import MODEL_FEATURES
from src.logger import get_logger
from src.observability import LogBuffer, RequestMetrics, process_snapshot
from src.official_fpl import OfficialFPLAPIError, OfficialFPLNotFoundError
from src.team_rating import rate_manager_team
from src.transfers import clubs_over_limit, currently_available, suggest_transfers

logger = get_logger(__name__)

ENVIRONMENT_FLAGS = (
    "OPENFPL_ENV",
    "K_SERVICE",
    "K_REVISION",
    "PORT",
    "OPENFPL_DATA_ROOT",
    "OPENFPL_DATA_ARCHIVE_ENABLED",
    "OPENFPL_DATA_ARCHIVE_ROOT",
    "FPL_DATA_INFERENCE_ENABLED",
    "FPL_DATA_ACKNOWLEDGE_PERMISSION_PENDING",
    "OPENFPL_LOG_FORMAT",
    "OPENFPL_ADMIN_ENABLED",
    "OPENFPL_SITE_URL",
)


def admin_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    """The kill switch: OPENFPL_ADMIN_ENABLED=false turns the dashboard off."""
    values = os.environ if environment is None else environment
    return values.get("OPENFPL_ADMIN_ENABLED", "true").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


class _Context:
    scout_provider: Callable[[], Any] = staticmethod(lambda: None)
    config: Mapping[str, Any] = {}
    metrics: Optional[RequestMetrics] = None
    log_buffer: Optional[LogBuffer] = None
    is_production: bool = False


context = _Context()
# Accuracy for gameweeks whose data official FPL has checked never changes.
_accuracy_cache: Dict[Any, Dict[str, Any]] = {}


def bind(
    scout_provider: Callable[[], Any],
    *,
    config: Mapping[str, Any],
    metrics: Optional[RequestMetrics] = None,
    log_buffer: Optional[LogBuffer] = None,
    is_production: bool = False,
) -> None:
    """Connect the router to the running app (called once by ``main``)."""
    context.scout_provider = scout_provider
    context.config = config
    context.metrics = metrics
    context.log_buffer = log_buffer
    context.is_production = is_production


def _require_enabled() -> None:
    if not admin_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"


router = APIRouter(
    prefix="/api/admin",
    tags=["Admin"],
    dependencies=[
        Depends(_require_enabled),
        Depends(verify_api_key),
        Depends(_no_store),
    ],
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _scout() -> Any:
    scout = context.scout_provider()
    if scout is None:
        raise HTTPException(status_code=503, detail="The scout is still starting")
    return scout


async def _run(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking work off the event loop and map domain errors to HTTP."""
    from src.scout import InferenceError

    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except HTTPException:
        raise
    except OfficialFPLNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except OfficialFPLAPIError as error:
        logger.exception("Official FPL request failed in the admin API")
        raise HTTPException(status_code=502, detail=str(error)) from error
    except (InferenceError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _json(value: Any) -> Any:
    """Strict JSON (no NaN) for payloads built from pandas/numpy values."""
    from src.data_archive import _json_safe

    return _json_safe(value)


def _resolve_gameweek(scout: Any, gameweek: Optional[int]) -> int:
    return int(gameweek or scout.official_client.next_gameweek())


def _predictions(scout: Any, gameweek: int) -> pd.DataFrame:
    return scout.get_official_predictions(
        gameweek, collect_results=False, allow_stale=False
    )


def _official_players(scout: Any) -> Dict[int, Dict[str, Any]]:
    mapped_players = getattr(scout.official_client, "mapped_players", None)
    if not callable(mapped_players):
        return {}
    try:
        return {int(player["id"]): player for player in mapped_players()}
    except Exception as error:
        logger.warning("Admin API could not read official players: %s", error)
        return {}


def _forecast_meta(predictions: pd.DataFrame) -> Dict[str, Any]:
    attrs = predictions.attrs
    inference = attrs.get("inference", {}) or {}
    return {
        "gameweek": int(attrs.get("gameweek") or 0),
        "frozen": bool(attrs.get("frozen", False)),
        "forecast_captured_at": attrs.get("forecast_captured_at"),
        "freeze_method": attrs.get("freeze_method"),
        "refresh_due": bool(attrs.get("refresh_due", False)),
        "strategy": inference.get("strategy", "model-ensemble"),
        "source": attrs.get("source", "official-fpl"),
        "successful_models": inference.get("successful_models", []),
        "failed_models": inference.get("failed_models", {}),
        "disabled_models": inference.get("disabled_models", []),
        "weights": inference.get("weights", {}),
        "mean_model_spread": inference.get("mean_model_spread"),
        "feature_coverage": inference.get("feature_coverage"),
        "enrichment": inference.get("data_enrichment"),
        "has_model_predictions": bool(inference.get("model_predictions")),
        "archive": attrs.get("archive"),
    }


def _inputs_meta(inputs: Any) -> Dict[str, Any]:
    return {
        "origin": inputs.origin,
        "approximate": bool(inputs.approximate),
        "built_at": inputs.built_at,
        "players": int(len(inputs.players)),
        "fixture_rows": int(len(inputs.fixture_rows)),
    }


def _safe_inputs(scout: Any, gameweek: int) -> tuple[Any, Optional[str]]:
    try:
        return scout.model_inputs(gameweek), None
    except Exception as error:  # Inputs enrich the view; they are optional.
        logger.info("Model inputs unavailable for GW%d: %s", gameweek, error)
        return None, str(error)


def _live_actuals(scout: Any, gameweek: int) -> Dict[int, Dict[str, float]]:
    event_live = getattr(scout.official_client, "event_live", None)
    if not callable(event_live):
        return {}
    try:
        return insights.actual_points(event_live(gameweek))
    except Exception:
        return {}


def _team_records(team: pd.DataFrame, official: Mapping[int, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    records = []
    for record in json.loads(team.to_json(orient="records")):
        details = official.get(int(record.get("id") or 0), {})
        for field in (
            "team_id",
            "position",
            "price",
            "selected_by_percent",
            "status",
            "news",
            "chance_of_playing_next_round",
            "total_points",
        ):
            if field in details and field not in record:
                record[field] = details[field]
        records.append(record)
    return records


# ----------------------------------------------------------------------
# Read routes
# ----------------------------------------------------------------------


def _overview() -> Dict[str, Any]:
    scout = _scout()
    client = scout.official_client
    gameweeks: Dict[str, Any] = {}
    try:
        events = client.mapped_gameweeks()
        current = next((event for event in events if event.get("is_current")), None)
        upcoming = next((event for event in events if event.get("is_next")), None)
        gameweeks = {
            "current": current,
            "next": upcoming,
            "open_gameweek": client.next_gameweek(),
            "finished": sum(1 for event in events if event.get("finished")),
            "total": len(events),
        }
    except Exception as error:
        gameweeks = {"error": str(error)}
    try:
        event_status = dict(client.event_status())
    except Exception as error:
        event_status = {"error": str(error)}

    upstream = (
        client.stats_snapshot() if callable(getattr(client, "stats_snapshot", None)) else None
    )
    requests_summary = None
    if context.metrics is not None:
        snapshot = context.metrics.snapshot()
        requests_summary = {
            key: snapshot[key]
            for key in (
                "since",
                "total_requests",
                "server_errors",
                "client_errors",
                "error_rate",
                "latency_ms",
            )
        }
        requests_summary["recent_errors"] = snapshot["recent_errors"][:5]
    states = scout.model_state()
    timeline = list(scout.forecast_events)[-8:]
    return _json(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": context.config.get("version"),
            "environment": {
                "production": context.is_production,
                "service": os.environ.get("K_SERVICE"),
                "revision": os.environ.get("K_REVISION"),
            },
            "process": process_snapshot(),
            "gameweeks": gameweeks,
            "event_status": event_status,
            "models": {
                "loaded": len(scout.model_artifacts),
                "enabled": sum(1 for state in states if state["enabled"]),
                "minimum_successful": scout.minimum_successful_models,
                "overrides_active": bool(scout.weight_overrides or scout.disabled_models),
                "state": states,
            },
            "last_inference": scout.last_inference,
            "live_forecasts": scout.live_forecast_state(),
            "enrichment": {
                "enabled": scout.fpl_data_enabled,
                "last_result": scout.last_data_enrichment,
            },
            "archive": scout.data_archive.status(),
            "requests": requests_summary,
            "upstream": upstream,
            "logs": {
                "counts": context.log_buffer.counts() if context.log_buffer else {},
                # Admin actions are audited at WARNING but are not problems;
                # they appear in the decision log instead.
                "recent_problems": [
                    record
                    for record in context.log_buffer.records("WARNING", 40)
                    if not record["message"].startswith("Admin action")
                ][:6]
                if context.log_buffer
                else [],
            },
            "timeline": list(reversed(timeline)),
        }
    )


@router.get("/overview", summary="Command-centre overview")
async def admin_overview():
    return await _run(_overview)


def _model_file(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"exists": False}
    return {
        "exists": True,
        "bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _models(gameweek: Optional[int]) -> Dict[str, Any]:
    scout = _scout()
    configured = scout.config.get("models", {}) or {}
    last = scout.last_inference or {}
    states = {state["name"]: state for state in scout.model_state()}
    cards = []
    for artifact in scout.model_artifacts:
        metadata = configured.get(artifact.name, {}) or {}
        estimator = artifact.estimator
        expected = getattr(estimator, "feature_names_in_", None)
        steps = getattr(estimator, "steps", None)
        if artifact.name in (last.get("failed_models") or {}):
            run_status = "failed"
        elif artifact.name in (last.get("disabled_models") or []):
            run_status = "disabled"
        elif artifact.name in (last.get("successful_models") or []):
            run_status = "ok"
        else:
            run_status = "not-run"
        cards.append(
            {
                **states.get(artifact.name, {}),
                "path": str(artifact.path),
                "file": _model_file(Path(artifact.path)),
                "version": metadata.get("version"),
                "last_trained": metadata.get("last_trained"),
                "year": metadata.get("year"),
                "estimator": type(estimator).__name__,
                "steps": [
                    [str(name), type(step).__name__] for name, step in steps
                ]
                if isinstance(steps, (list, tuple))
                else None,
                "feature_contract": {
                    "declared": expected is not None,
                    "features": len(expected) if expected is not None else None,
                    "matches": expected is None or list(expected) == MODEL_FEATURES,
                },
                "last_run": {
                    "status": run_status,
                    "error": (last.get("failed_models") or {}).get(artifact.name),
                    "at": last.get("at"),
                },
            }
        )
    resolved = _resolve_gameweek(scout, gameweek)
    agreement: Dict[str, Any]
    meta: Dict[str, Any] = {}
    try:
        predictions = _predictions(scout, resolved)
        agreement = insights.model_agreement(predictions)
        meta = _forecast_meta(predictions)
    except Exception as error:
        agreement = {"available": False, "error": str(error)}
    provider = scout.fpl_data_provider
    provider_status = (
        provider.status() if callable(getattr(provider, "status", None)) else None
    )
    return _json(
        {
            "gameweek": resolved,
            "models": cards,
            "ensemble": {
                "minimum_successful_models": scout.minimum_successful_models,
                "history_window": scout.history_window,
                "clip_min": scout.clip_min,
                "clip_max": scout.clip_max,
                "cold_start_enabled": scout.cold_start_enabled,
                "max_players_per_team": scout.max_players_per_team,
                "prediction_cache_ttl_seconds": scout.prediction_cache_ttl,
                "prediction_stale_seconds": scout.prediction_stale_ttl,
                "effective_weights": scout.effective_weights(),
            },
            "feature_contract": MODEL_FEATURES,
            "feature_coverage": meta.get("feature_coverage") or last.get("feature_coverage"),
            "last_inference": last,
            "forecast": meta,
            "agreement": agreement,
            "enrichment": {
                "enabled": scout.fpl_data_enabled,
                "season": scout.fpl_data_season,
                "start_gameweek": scout.fpl_data_start_gameweek,
                "permission_status": scout.fpl_data_permission_status,
                "permission_pending_acknowledged": scout.fpl_data_permission_acknowledged,
                "last_result": scout.last_data_enrichment,
                "provider": provider_status,
            },
        }
    )


@router.get("/models", summary="Model health, agreement and enrichment")
async def admin_models(gameweek: Optional[int] = Query(None, ge=1, le=38)):
    return await _run(_models, gameweek)


def _predictions_table(gameweek: Optional[int]) -> Dict[str, Any]:
    scout = _scout()
    resolved = _resolve_gameweek(scout, gameweek)
    predictions = _predictions(scout, resolved)
    team, trace = insights.select_with_trace(
        predictions, max_per_club=scout.max_players_per_team
    )
    inputs, inputs_error = _safe_inputs(scout, resolved)
    rows = insights.prediction_table(
        predictions,
        inputs_players=inputs.players if inputs is not None else None,
        official_players=_official_players(scout),
        trace=trace,
        team=team,
    )
    return _json(
        {
            **_forecast_meta(predictions),
            "inputs": _inputs_meta(inputs) if inputs is not None else None,
            "inputs_error": inputs_error,
            "models": [artifact.name for artifact in scout.model_artifacts],
            "table_features": insights.TABLE_FEATURES,
            "feature_labels": insights.FEATURE_LABELS,
            "count": len(rows),
            "players": rows,
        }
    )


@router.get("/predictions", summary="Every player's projection with its inputs")
async def admin_predictions(gameweek: Optional[int] = Query(None, ge=1, le=38)):
    return await _run(_predictions_table, gameweek)


def _explain(player_id: int, gameweek: Optional[int]) -> Dict[str, Any]:
    scout = _scout()
    resolved = _resolve_gameweek(scout, gameweek)
    predictions = _predictions(scout, resolved)
    meta = _forecast_meta(predictions)
    if meta["strategy"] == "ownership-cold-start":
        try:
            return _json({**insights.cold_start_explanation(predictions, player_id), "forecast": meta})
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Player not in this forecast") from error
    team, trace = insights.select_with_trace(
        predictions, max_per_club=scout.max_players_per_team
    )
    inputs = scout.model_inputs(resolved)
    official = _official_players(scout).get(int(player_id))
    try:
        explanation = insights.explain_player(
            scout,
            inputs,
            predictions,
            int(player_id),
            trace=trace,
            team=team,
            official=official,
        )
    except KeyError as error:
        raise HTTPException(
            status_code=404, detail=f"Player {player_id} has no GW{resolved} model inputs"
        ) from error
    served = explanation.get("served_points")
    recomputed = explanation.get("expected_points")
    notes = []
    if inputs.approximate:
        notes.append(
            "Inputs were rebuilt after the forecast "
            f"({inputs.origin}); availability is current, so numbers can differ "
            "from the served forecast."
        )
    if meta["frozen"]:
        notes.append(
            "This gameweek is frozen: the served projection is the pre-deadline "
            "forecast; the explanation re-runs today's models on its inputs."
        )
    if scout.weight_overrides or scout.disabled_models:
        notes.append("Runtime model overrides are active.")
    if served is not None and recomputed is not None and abs(served - recomputed) > 0.05:
        notes.append(
            f"Served projection {served:.2f} differs from this re-run ({recomputed:.2f})."
        )
    return _json({**explanation, "forecast": meta, "notes": notes, "official": official})


@router.get("/players/{player_id}/explain", summary="Explain one player's projection")
async def admin_explain(
    player_id: int, gameweek: Optional[int] = Query(None, ge=1, le=38)
):
    return await _run(_explain, player_id, gameweek)


def _squad(gameweek: Optional[int]) -> Dict[str, Any]:
    scout = _scout()
    resolved = _resolve_gameweek(scout, gameweek)
    predictions = _predictions(scout, resolved)
    team, trace = insights.select_with_trace(
        predictions, max_per_club=scout.max_players_per_team
    )
    official = _official_players(scout)
    records = _team_records(team, official)
    decisions = trace["decisions"]
    actuals = _live_actuals(scout, resolved)
    model_points = insights.model_points_by_id(predictions.attrs.get("inference", {}))
    for record in records:
        player_id = int(record["id"])
        decision = decisions.get(player_id, {})
        record["decision_reason"] = decision.get("reason")
        record["rank_in_position"] = decision.get("rank_in_position")
        record["models"] = model_points.get(player_id, {})
        if actuals:
            actual = actuals.get(player_id) or {}
            record["actual_points"] = actual.get("points", 0.0)
            record["actual_minutes"] = actual.get("minutes", 0.0)
    lineup = insights.lineup_summary(records)
    result = {
        **_forecast_meta(predictions),
        "squad": records,
        "lineup": lineup,
        "captaincy": insights.captaincy_reasoning(team),
        "cutoffs": trace["cutoffs"],
        "near_misses": trace["near_misses"],
        "club_limited": trace["club_limited"],
        "max_per_club": trace["max_per_club"],
        "club_counts": trace["club_counts"],
        "projected_squad_total": round(float(team["expected_points"].sum()), 2),
    }
    if actuals:
        starting = set(lineup["starting_ids"])
        captain = lineup["captain_id"]
        result["actual"] = {
            "starting_xi": round(
                sum(
                    (actuals.get(int(pid)) or {}).get("points", 0.0) for pid in starting
                ),
                1,
            ),
            "captain_bonus": (actuals.get(int(captain)) or {}).get("points", 0.0)
            if captain is not None
            else 0.0,
        }
        result["actual"]["total"] = round(
            result["actual"]["starting_xi"] + result["actual"]["captain_bonus"], 1
        )
    return _json(result)


@router.get("/squad", summary="The AI squad with the reason for every pick")
async def admin_squad(gameweek: Optional[int] = Query(None, ge=1, le=38)):
    return await _run(_squad, gameweek)


def _accuracy(played_only: bool) -> Dict[str, Any]:
    scout = _scout()
    client = scout.official_client
    archive = scout.data_archive
    if not archive.enabled:
        return {"archive_enabled": False, "gameweeks": [], "summary": {}, "missing": []}
    events = client.mapped_gameweeks()
    results = []
    missing = []
    for event in events:
        if not event.get("finished") and not event.get("is_current"):
            continue
        gameweek = int(event["id"])
        forecast = archive.archived_forecast(client, gameweek)
        if forecast is None:
            missing.append(gameweek)
            continue
        key = (gameweek, forecast.attrs.get("forecast_captured_at"), played_only)
        cached = _accuracy_cache.get(key)
        if cached is None:
            live = _live_actuals(scout, gameweek)
            if not live:
                missing.append(gameweek)
                continue
            cached = insights.gameweek_accuracy(
                forecast,
                live,
                select=scout.select_optimal_team,
                event=event,
                played_only=played_only,
            )
            cached["finished"] = bool(event.get("finished"))
            cached["data_checked"] = bool(event.get("data_checked"))
            if event.get("data_checked"):
                _accuracy_cache[key] = cached
        results.append(copy.deepcopy(cached))
    summary = insights.accuracy_summary(results)
    return _json(
        {
            "archive_enabled": True,
            "played_only": played_only,
            "gameweeks": results,
            "summary": summary,
            "missing": missing,
        }
    )


@router.get("/accuracy", summary="Archived forecasts against actual points")
async def admin_accuracy(played_only: bool = Query(False)):
    return await _run(_accuracy, played_only)


@router.get("/timeline", summary="AI decision log and admin audit trail")
async def admin_timeline(limit: int = Query(100, ge=1, le=200)):
    scout = _scout()
    return _json({"events": list(reversed(list(scout.forecast_events)))[:limit]})


@router.get("/logs", summary="Recent application log records")
async def admin_logs(
    level: str = Query("INFO"),
    q: Optional[str] = Query(None),
    logger_name: Optional[str] = Query(None, alias="logger"),
    limit: int = Query(200, ge=1, le=1000),
):
    if context.log_buffer is None:
        return {"records": [], "counts": {}}
    return {
        "records": context.log_buffer.records(level, limit, q, logger_name),
        "counts": context.log_buffer.counts(),
    }


def _metrics() -> Dict[str, Any]:
    scout = _scout()
    client = scout.official_client
    with scout._forecast_lock:
        inputs = [
            {"gameweek": gameweek, **_inputs_meta(entry)}
            for gameweek, entry in scout._model_inputs.items()
        ]
    with scout._fixture_cache_lock:
        fixture_cache = len(scout._fixture_cache)
    return _json(
        {
            "requests": context.metrics.snapshot() if context.metrics else None,
            "upstream": client.stats_snapshot()
            if callable(getattr(client, "stats_snapshot", None))
            else None,
            "official_cache": client.cache_status()
            if callable(getattr(client, "cache_status", None))
            else None,
            "fixture_cache_entries": fixture_cache,
            "live_forecasts": scout.live_forecast_state(),
            "model_inputs": inputs,
            "process": process_snapshot(),
        }
    )


@router.get("/metrics", summary="Request, upstream and cache metrics")
async def admin_metrics():
    return await _run(_metrics)


@router.get("/archive", summary="What the season archive holds")
async def admin_archive():
    scout = _scout()
    return await _run(
        lambda: _json(scout.data_archive.inventory(scout.official_client))
    )


@router.get("/config", summary="Effective configuration (no secrets)")
async def admin_config():
    scout = _scout()
    return _json(
        {
            "config": copy.deepcopy(dict(context.config)),
            "environment": {name: os.environ.get(name) for name in ENVIRONMENT_FLAGS},
            "api_keys_configured": len(auth_module.VALID_API_KEYS),
            "runtime": {
                "weight_overrides": scout.weight_overrides,
                "disabled_models": sorted(scout.disabled_models),
                "fpl_data_enabled": scout.fpl_data_enabled,
                "archive_enabled": scout.data_archive.enabled,
                "archive_root": str(scout.data_archive.root_path),
            },
        }
    )


# ----------------------------------------------------------------------
# Live controls (change the running service until restart)
# ----------------------------------------------------------------------


def _recompute(gameweek: Optional[int]) -> Dict[str, Any]:
    scout = _scout()
    resolved = _resolve_gameweek(scout, gameweek)
    try:
        predictions = scout.recompute(resolved)
    except ValueError as error:
        if "frozen" in str(error):
            raise HTTPException(status_code=409, detail=str(error)) from error
        raise
    prediction_event = next(
        (
            event
            for event in reversed(list(scout.forecast_events))
            if event.get("type") == "prediction" and event.get("gameweek") == resolved
        ),
        None,
    )
    scout.record_admin_event("recompute", gameweek=resolved)
    return _json(
        {"gameweek": resolved, "forecast": _forecast_meta(predictions), "event": prediction_event}
    )


@router.post("/predictions/recompute", summary="Live: predict an open gameweek again now")
async def admin_recompute(gameweek: Optional[int] = Query(None, ge=1, le=38)):
    return await _run(_recompute, gameweek)


@router.post("/archive/collect", summary="Live: collect official results into the archive")
async def admin_collect(force: bool = Query(True)):
    scout = _scout()

    def collect() -> Dict[str, Any]:
        result = scout.data_archive.collect_results(scout.official_client, force=force)
        scout.record_admin_event("collect-results", force=force, status=result.get("status"))
        return _json(result)

    return await _run(collect)


class CacheClearRequest(BaseModel):
    official: bool = True
    fixtures: bool = True
    forecasts: bool = True


@router.post("/cache/clear", summary="Live: clear caches")
async def admin_clear_cache(request: CacheClearRequest = Body(default_factory=CacheClearRequest)):
    scout = _scout()
    result: Dict[str, Any] = {}
    if request.official and callable(getattr(scout.official_client, "clear_cache", None)):
        status = scout.official_client.cache_status() if callable(
            getattr(scout.official_client, "cache_status", None)
        ) else {"entries": None}
        scout.official_client.clear_cache()
        result["official_entries_cleared"] = status.get("entries")
    if request.fixtures:
        result["fixture_entries_cleared"] = scout.clear_fixture_cache()
    if request.forecasts:
        result["forecasts_invalidated"] = scout.invalidate_live_forecasts()
    scout.record_admin_event("clear-cache", **result)
    return result


class ModelOverrideRequest(BaseModel):
    weight: Optional[float] = Field(None, gt=0)
    enabled: Optional[bool] = None


@router.post("/models/reset", summary="Live: remove every runtime model override")
async def admin_reset_models():
    scout = _scout()
    state = scout.reset_model_overrides()
    scout.record_admin_event("reset-model-overrides")
    return {"models": state}


@router.post("/models/{name}", summary="Live: set a model's runtime weight or enabled flag")
async def admin_set_model(name: str, request: ModelOverrideRequest):
    scout = _scout()
    try:
        state = scout.set_model_override(name, weight=request.weight, enabled=request.enabled)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"Unknown model {name!r}") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    scout.record_admin_event(
        "model-override", model=name, weight=request.weight, enabled=request.enabled
    )
    return {"models": state}


class EnrichmentRequest(BaseModel):
    enabled: bool


@router.post("/enrichment", summary="Live: turn FPL Data enrichment on or off")
async def admin_enrichment(request: EnrichmentRequest):
    scout = _scout()
    result = await _run(scout.set_enrichment_enabled, request.enabled)
    scout.record_admin_event("enrichment", enabled=request.enabled)
    return result


# ----------------------------------------------------------------------
# Sandbox (never changes the live service)
# ----------------------------------------------------------------------


class SimulationRequest(BaseModel):
    gameweek: Optional[int] = Field(None, ge=1, le=38)
    weights: Dict[str, float] = Field(default_factory=dict)
    disabled: List[str] = Field(default_factory=list)
    exclude_ids: List[int] = Field(default_factory=list)
    lock_ids: List[int] = Field(default_factory=list)
    max_per_club: int = Field(3, ge=1, le=15)
    availability_overrides: Dict[int, float] = Field(default_factory=dict)


def _simulate(request: SimulationRequest) -> Dict[str, Any]:
    scout = _scout()
    names = {artifact.name for artifact in scout.model_artifacts}
    unknown = sorted((set(request.weights) | set(request.disabled)) - names)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown models: {unknown}")
    if any(not np.isfinite(value) or value < 0 for value in request.weights.values()):
        raise HTTPException(status_code=422, detail="Weights must be zero or positive")
    disabled = set(request.disabled) | {
        name for name, value in request.weights.items() if value == 0
    }
    if disabled >= names:
        raise HTTPException(status_code=422, detail="At least one model must stay on")
    resolved = _resolve_gameweek(scout, request.gameweek)
    inputs = scout.model_inputs(resolved)
    weights = {name: value for name, value in request.weights.items() if value > 0}
    sandbox = scout.predict_from_inputs(
        inputs,
        weights=weights or None,
        disabled=disabled,
        availability_overrides=request.availability_overrides,
    )
    team, trace = insights.select_with_trace(
        sandbox,
        max_per_club=request.max_per_club,
        locks=request.lock_ids,
        excludes=request.exclude_ids,
    )
    live = _predictions(scout, resolved)
    live_team = scout.select_optimal_team(live)
    official = _official_players(scout)
    records = _team_records(team, official)
    for record in records:
        decision = trace["decisions"].get(int(record["id"]), {})
        record["decision_reason"] = decision.get("reason")
        record["decision"] = decision.get("status")
    live_points = live.drop_duplicates("id").set_index("id")["expected_points"]
    sandbox_points = sandbox.drop_duplicates("id").set_index("id")["expected_points"]
    shared = live_points.index.intersection(sandbox_points.index)
    deltas = (sandbox_points.loc[shared] - live_points.loc[shared]).dropna()
    labels = sandbox.drop_duplicates("id").set_index("id")
    movers = [
        {
            "id": int(player_id),
            "web_name": labels.loc[player_id, "web_name"],
            "team_name": labels.loc[player_id, "team_name"],
            "live": round(float(live_points.loc[player_id]), 3),
            "sandbox": round(float(sandbox_points.loc[player_id]), 3),
            "delta": round(float(deltas.loc[player_id]), 3),
        }
        for player_id in deltas.abs().sort_values(ascending=False).head(15).index
    ]
    rejected = {
        key: value
        for key, value in trace["decisions"].items()
        if value.get("status") == "lock-rejected"
    }
    return _json(
        {
            "gameweek": resolved,
            "sandbox": True,
            "inputs": _inputs_meta(inputs),
            "weights": sandbox.attrs["inference"].get("weights", {}),
            "successful_models": sandbox.attrs["inference"].get("successful_models", []),
            "squad": records,
            "lineup": insights.lineup_summary(records),
            "captaincy": insights.captaincy_reasoning(team),
            "cutoffs": trace["cutoffs"],
            "near_misses": trace["near_misses"],
            "club_counts": trace["club_counts"],
            "rejected_locks": rejected,
            "diff": insights.squad_diff(live_team, team),
            "movers": movers,
        }
    )


@router.post("/simulate", summary="Sandbox: re-select the squad under what-if settings")
async def admin_simulate(request: SimulationRequest):
    return await _run(_simulate, request)




# ----------------------------------------------------------------------
# My team: a manager's working squad, advice and planning
# (plans only: nothing here changes a team on FPL)
# ----------------------------------------------------------------------

PLAN_CAVEATS = [
    "Official FPL publishes a squad only after each deadline, so transfers already "
    "made for the coming gameweek are not included unless you add them to your "
    "working squad.",
    "Selling prices and free transfers are estimated from public history; FPL's own "
    "numbers can differ slightly.",
    "Prices are today's; price changes over the horizon are not predicted.",
]
OVERRIDE_LIMIT = 60
# Players a manager's override re-projects as fit, per gameweek and inputs.
_raw_cache: "Dict[Any, Dict[int, float]]" = {}
RAW_CACHE_SIZE = 32
RAW_FIELDS = (
    "news_added",
    "chance_of_playing_this_round",
    "transfers_in_event",
    "transfers_out_event",
    "cost_change_event",
    "cost_change_start",
)


class DraftPick(BaseModel):
    id: int = Field(..., ge=1)
    position: int = Field(..., ge=1, le=15)
    is_captain: bool = False
    is_vice_captain: bool = False


class Draft(BaseModel):
    """A working squad: published picks plus edits FPL cannot see yet."""

    picks: List[DraftPick] = Field(..., max_length=20)
    chip: Optional[Literal["bboost", "3xc", "freehit", "wildcard"]] = None
    free_transfers: Optional[int] = Field(None, ge=0, le=5)
    bank_override: Optional[float] = Field(None, ge=0, le=100)
    purchase_prices: Dict[int, float] = Field(default_factory=dict)


class PlayerOverride(BaseModel):
    """A manager's own view of one player."""

    lock: bool = False
    ban: bool = False
    availability: Optional[float] = Field(None, ge=0, le=100)
    xp_pct: Optional[float] = Field(None, ge=0, le=200)


def _overrides(raw: Mapping[int, PlayerOverride]) -> Dict[int, Dict[str, Any]]:
    if len(raw) > OVERRIDE_LIMIT:
        raise HTTPException(status_code=422, detail=f"At most {OVERRIDE_LIMIT} player overrides")
    overrides = {int(key): value.model_dump() for key, value in raw.items()}
    both = sorted(key for key, value in overrides.items() if value["lock"] and value["ban"])
    if both:
        raise HTTPException(
            status_code=422, detail=f"Players cannot be both locked and banned: {both}"
        )
    return overrides


def _draft_picks(draft: Draft) -> List[Dict[str, Any]]:
    for player_id, price in draft.purchase_prices.items():
        if not 3.0 <= float(price) <= 20.0:
            raise HTTPException(
                status_code=422,
                detail=f"Purchase price for #{player_id} must be between £3.0m and £20.0m",
            )
    return [pick.model_dump() for pick in draft.picks]


def _published_picks(client: Any, entry_id: int, gameweek: int) -> tuple:
    """The manager, the gameweek of their latest picks and those picks."""
    manager = client.mapped_manager(entry_id)
    current_event = manager.get("current_event")
    picks_gameweek = min(gameweek, int(current_event)) if current_event else 0
    if picks_gameweek < 1:
        raise HTTPException(
            status_code=422,
            detail="This manager has no published squad yet (it appears after the first deadline).",
        )
    payload = client.mapped_manager_picks(entry_id, picks_gameweek)
    picks = [
        {
            "id": int(pick["element"]),
            "position": int(pick.get("position") or 0),
            "is_captain": bool(pick.get("is_captain")),
            "is_vice_captain": bool(pick.get("is_vice_captain")),
            "multiplier": int(pick.get("multiplier") or 0),
        }
        for pick in payload.get("picks", [])
    ]
    return manager, picks_gameweek, sorted(picks, key=lambda pick: pick["position"]), payload


def _require_open(scout: Any, gameweek: int) -> None:
    client = scout.official_client
    if not scout.data_archive.gameweek_is_open(client, gameweek):
        raise HTTPException(
            status_code=409,
            detail=(
                f"GW{gameweek} is past its deadline, so its transfers are closed; "
                f"plan from GW{client.next_gameweek()}."
            ),
        )


def _raw_projections(scout: Any, gameweek: int, ids: List[int]) -> Dict[int, float]:
    """Projections as if fit for players ruled out (the sandbox path)."""
    ids = sorted(set(int(value) for value in ids))
    if not ids:
        return {}
    inputs = scout.model_inputs(gameweek)
    key = (int(gameweek), inputs.built_at, tuple(ids))
    if key in _raw_cache:
        return _raw_cache[key]
    frame = scout.predict_from_inputs(
        inputs, availability_overrides={player_id: 1.0 for player_id in ids}
    )
    values = frame.drop_duplicates("id").set_index("id")["expected_points"]
    result = {
        player_id: float(values.loc[player_id]) for player_id in ids if player_id in values.index
    }
    _raw_cache[key] = result
    while len(_raw_cache) > RAW_CACHE_SIZE:
        _raw_cache.pop(next(iter(_raw_cache)))
    return result


def _bootstrap_elements(scout: Any) -> tuple:
    """Raw official fields the mapped players leave out, and the player count."""
    try:
        bootstrap = scout.official_client.bootstrap()
    except Exception as error:
        logger.info("Raw official players unavailable: %s", error)
        return {}, None
    elements = {
        int(element["id"]): {field: element.get(field) for field in RAW_FIELDS}
        for element in bootstrap.get("elements", []) or []
        if element.get("id") is not None
    }
    return elements, bootstrap.get("total_players")


def _team_fixtures(scout: Any, gameweeks: List[int]) -> Dict[int, Dict[int, List[Dict[str, Any]]]]:
    """``{team_id: {gameweek: [fixture]}}`` from each team's point of view."""
    mapped_fixtures = getattr(scout.official_client, "mapped_fixtures", None)
    result: Dict[int, Dict[int, List[Dict[str, Any]]]] = {}
    if not callable(mapped_fixtures):
        return result
    wanted = set(int(value) for value in gameweeks)
    try:
        fixtures = mapped_fixtures()
    except Exception as error:
        logger.info("Fixtures unavailable for My team: %s", error)
        return result
    for fixture in fixtures:
        gameweek = fixture.get("gameweek")
        if gameweek is None or int(gameweek) not in wanted:
            continue
        home, away = fixture.get("home_team") or {}, fixture.get("away_team") or {}
        for side, other, is_home in ((home, away, True), (away, home, False)):
            if side.get("id") is None:
                continue
            result.setdefault(int(side["id"]), {}).setdefault(int(gameweek), []).append(
                {
                    "opponent": other.get("name"),
                    "opponent_short": other.get("short_name"),
                    "home": is_home,
                    "difficulty": side.get("difficulty"),
                    "kickoff_time": fixture.get("kickoff_time"),
                    "finished": fixture.get("finished"),
                }
            )
    return result


def _manager_plan_context(
    entry_id: int, gameweek: Optional[int], draft: Optional[Draft] = None
) -> Dict[str, Any]:
    """Everything the planner needs about a manager's (working) squad.

    From public data, with a draft's transfers, purchase-price fixes, bank
    override and free-transfer count applied when one is given.
    """
    scout = _scout()
    client = scout.official_client
    resolved = _resolve_gameweek(scout, gameweek)
    _require_open(scout, resolved)
    manager, picks_gameweek, published_picks, _ = _published_picks(client, entry_id, resolved)
    history = client.manager_history(entry_id)
    chips = history.get("chips", []) or []
    transfers = client.mapped_manager_transfers(entry_id)
    official = _official_players(scout)
    started_event = manager.get("started_event")

    published_ids = [pick["id"] for pick in published_picks]
    working_picks = _draft_picks(draft) if draft is not None else [
        {key: pick[key] for key in ("id", "position", "is_captain", "is_vice_captain")}
        for pick in published_picks
    ]
    working_ids = list(dict.fromkeys(int(pick["id"]) for pick in working_picks))
    start_prices: Dict[int, Optional[float]] = {}
    for player_id in published_ids:
        try:
            rows = client.mapped_player_summary(player_id).get("history", [])
        except Exception:  # A departed player has no summary.
            rows = []
        first = next(
            (
                row
                for row in sorted(rows, key=lambda item: item.get("gameweek") or 0)
                if started_event is None or (row.get("gameweek") or 0) >= int(started_event)
            ),
            None,
        )
        start_prices[player_id] = first.get("price") if first else None
    bought = optimizer.purchase_prices(published_ids, transfers, chips, started_event, start_prices)
    fixes = {int(key): float(value) for key, value in (draft.purchase_prices if draft else {}).items()}

    def purchase_of(player_id: int) -> tuple:
        now = official.get(player_id, {}).get("price")
        if player_id not in bought:
            return now, "bought in your working squad"
        if player_id in fixes:
            return fixes[player_id], "your price"
        return bought[player_id]["price"], bought[player_id]["source"]

    selling: Dict[int, Optional[float]] = {}
    squad = []
    picks_by_id = {int(pick["id"]): pick for pick in working_picks}
    for player_id in dict.fromkeys(published_ids + working_ids):
        details = official.get(player_id, {})
        now = details.get("price")
        purchase, source = purchase_of(player_id)
        selling[player_id] = optimizer.selling_price(purchase, now)
        if player_id not in picks_by_id or not details:
            continue
        pick = picks_by_id[player_id]
        squad.append(
            {
                "id": player_id,
                "web_name": details.get("web_name"),
                "team_name": details.get("team_name"),
                "element_type": details.get("element_type"),
                "position": insights.POSITION_SHORT.get(int(details.get("element_type") or 0), "?"),
                "pick_position": int(pick.get("position") or 0),
                "is_captain": bool(pick.get("is_captain")),
                "is_vice_captain": bool(pick.get("is_vice_captain")),
                "price": now,
                "purchase_price": purchase,
                "purchase_source": source,
                "selling_price": selling[player_id],
                "new": player_id not in bought,
                "status": details.get("status"),
                "news": details.get("news") or "",
                "can_select": details.get("can_select"),
            }
        )
    bank = manager.get("last_deadline_bank")
    bank = float(bank) if bank is not None else 0.0
    estimate = optimizer.estimate_free_transfers(
        history.get("current", []), chips, started_event, resolved
    )
    free = estimate.get("free_transfers")
    if draft is not None and draft.free_transfers is not None and not estimate.get("unlimited"):
        free = int(draft.free_transfers)
    changes = None
    remaining = free
    working_bank = bank
    if draft is not None:
        changes = my_team.squad_changes(
            published_picks,
            working_picks,
            players=official,
            selling=selling,
            bank=bank,
            free_transfers=free,
            chip=draft.chip,
            bank_override=draft.bank_override,
        )
        remaining = changes["free_transfers_remaining"]
        working_bank = changes["bank_after"]
    squad_value = round(
        sum(float(row["selling_price"] or 0) for row in squad if row["id"] in picks_by_id), 1
    )
    return {
        "entry_id": entry_id,
        "team_name": manager.get("name"),
        "gameweek": resolved,
        "picks_gameweek": picks_gameweek,
        "transfers_open": True,
        "edited": draft is not None,
        "bank": round(working_bank, 1),
        "published_bank": round(bank, 1),
        "squad_value": squad_value,
        "budget": round(working_bank + squad_value, 1),
        "squad": sorted(squad, key=lambda row: row["pick_position"]),
        "published_picks": published_picks,
        "working_picks": working_picks,
        "selling": selling,
        "free_transfers": estimate,
        "free_transfers_used": free,
        "free_transfers_remaining": remaining,
        "changes": changes,
        "chips": optimizer.chip_status(chips, resolved),
        "chip": draft.chip if draft is not None else None,
        "caveats": PLAN_CAVEATS,
    }


@router.get(
    "/managers/{entry_id}/plan-context",
    summary="Estimated free transfers, selling prices and budget for planning",
)
async def admin_plan_context(
    entry_id: int, gameweek: Optional[int] = Query(None, ge=1, le=38)
):
    def build() -> Dict[str, Any]:
        result = _manager_plan_context(entry_id, gameweek)
        result.pop("selling", None)
        return _json(result)

    return await _run(build)


class TeamRequest(BaseModel):
    gameweek: Optional[int] = Field(None, ge=1, le=38)
    draft: Optional[Draft] = None
    overrides: Dict[int, PlayerOverride] = Field(default_factory=dict)


OFFICIAL_RECORD_FIELDS = (
    "team_id",
    "position",
    "price",
    "selected_by_percent",
    "status",
    "can_select",
    "news",
    "chance_of_playing_next_round",
    "total_points",
)


def _working_team(entry_id: int, request: TeamRequest) -> Dict[str, Any]:
    """The working squad scored, checked and advised on, beside the published one."""
    scout = _scout()
    client = scout.official_client
    resolved = _resolve_gameweek(scout, request.gameweek)
    is_open = scout.data_archive.gameweek_is_open(client, resolved)
    draft = request.draft
    if draft is not None and not is_open:
        _require_open(scout, resolved)
    overrides = _overrides(request.overrides)
    if is_open:
        plan_context = _manager_plan_context(entry_id, resolved, draft)
        manager = client.mapped_manager(entry_id)
        picks_gameweek = plan_context["picks_gameweek"]
        published_picks = plan_context["published_picks"]
        working_picks = plan_context["working_picks"]
    else:
        plan_context = None
        manager, picks_gameweek, published_picks, _ = _published_picks(client, entry_id, resolved)
        working_picks = [
            {key: pick[key] for key in ("id", "position", "is_captain", "is_vice_captain")}
            for pick in published_picks
        ]
    chip = draft.chip if draft is not None else None

    predictions = _predictions(scout, resolved)
    prediction_gameweek = int(predictions.attrs.get("gameweek") or resolved)
    official = _official_players(scout)
    elements, total_players = _bootstrap_elements(scout)
    fixtures = _team_fixtures(scout, [prediction_gameweek])
    records = json.loads(predictions.drop_duplicates("id").to_json(orient="records"))
    base_by_id = {int(record["id"]): record for record in records if record.get("id") is not None}
    model_points = insights.model_points_by_id(predictions.attrs.get("inference", {}) or {})
    need = [
        player_id
        for player_id, override in overrides.items()
        if player_id in base_by_id
        and my_team.needs_raw(base_by_id[player_id].get("availability_factor"), override)
    ]
    notes: List[str] = []
    raw: Dict[int, float] = {}
    if need:
        try:
            raw = _raw_projections(scout, prediction_gameweek, need)
        except Exception as error:
            notes.append(f"Players ruled out could not be re-projected as fit ({error}).")
    adjusted, adjusted_models, override_notes = my_team.apply_overrides(
        records, overrides, raw, model_points
    )
    notes.extend(override_notes)
    by_id = {int(record["id"]): record for record in adjusted if record.get("id") is not None}

    def record_for(pick: Mapping[str, Any], source: Mapping[int, Mapping[str, Any]], multiplier: int) -> Dict[str, Any]:
        player_id = int(pick["id"])
        details = official.get(player_id, {})
        prediction = source.get(player_id)
        if prediction is None:
            # As in the public team rating: players without a projection
            # count, with no points and no availability.
            prediction = {"expected_points": 0.0, "availability_factor": 0.0, "projection_missing": True}
        return {
            **details,
            **prediction,
            "id": player_id,
            "pick_position": int(pick.get("position") or 0),
            "multiplier": multiplier,
            "is_captain": bool(pick.get("is_captain")),
            "is_vice_captain": bool(pick.get("is_vice_captain")),
            "role": "captain" if pick.get("is_captain") else "vice" if pick.get("is_vice_captain") else "",
        }

    benchmark = scout.select_optimal_team(predictions)
    benchmark_records = json.loads(benchmark.to_json(orient="records"))
    published_records = [
        record_for(pick, base_by_id, int(pick.get("multiplier") or 0)) for pick in published_picks
    ]
    try:
        published_rating = rate_manager_team(published_records, benchmark_records)
    except ValueError:
        published_rating = None

    chip_status = plan_context["chips"]["chips"] if plan_context else None
    changes = plan_context["changes"] if plan_context else None
    validation = my_team.validate_squad(
        working_picks,
        official,
        published_ids=[pick["id"] for pick in published_picks],
        bank_after=changes["bank_after"] if changes else None,
        chip=chip,
        chip_status=chip_status,
    )
    working_multipliers = my_team.multipliers(working_picks, chip)
    known = [pick for pick in working_picks if int(pick["id"]) in official]
    working_records = [
        record_for(pick, by_id, working_multipliers[int(pick["id"])]) for pick in known
    ]
    players_map: Dict[int, Dict[str, Any]] = {}
    for record in working_records:
        team_id = record.get("team_id")
        record["fixtures"] = (fixtures.get(int(team_id), {}) if team_id is not None else {}).get(
            prediction_gameweek, []
        )
        players_map[int(record["id"])] = record
    structure_ok = (
        len(known) == 15
        and len({int(pick["id"]) for pick in known}) == 15
        and sum(1 for pick in known if int(pick["position"]) <= 11) == 11
    )
    rating = expected = advice = None
    captaincy: List[Dict[str, Any]] = []
    if structure_ok:
        try:
            rating = rate_manager_team(working_records, benchmark_records)
        except ValueError as error:
            notes.append(f"The working squad could not be rated: {error}")
        expected = my_team.expected_with_autosubs(players_map, working_picks, chip)
        if not validation["errors"] or all("budget" in item for item in validation["errors"]):
            try:
                advice = my_team.lineup_advice(players_map, working_picks, chip)
            except ValueError as error:
                notes.append(f"No lineup advice: {error}")
        captaincy = my_team.captaincy_options(players_map, working_picks, adjusted_models, chip=chip)

    locked = sorted(player_id for player_id, value in overrides.items() if value["lock"])
    banned = sorted(player_id for player_id, value in overrides.items() if value["ban"])
    suggestions: List[Dict[str, Any]] = []
    bank = plan_context["bank"] if plan_context else manager.get("last_deadline_bank")
    if is_open and structure_ok and validation["valid"]:
        candidates = [
            {
                **{
                    field: official.get(int(record["id"]), {}).get(field)
                    for field in OFFICIAL_RECORD_FIELDS
                    if field in official.get(int(record["id"]), {})
                },
                **record,
            }
            for record in adjusted
        ]
        suggestions = suggest_transfers(
            working_records,
            candidates,
            None if bank is None else float(bank),
            keep_ids=locked,
            exclude_ids=banned,
        )

    context_rows = {row["id"]: row for row in (plan_context["squad"] if plan_context else [])}
    bench_use = (expected or {}).get("bench_use", {})
    squad = []
    for pick in sorted(working_picks, key=lambda item: int(item["position"])):
        player_id = int(pick["id"])
        record = players_map.get(player_id)
        if record is None:
            squad.append({"id": player_id, "pick_position": int(pick["position"]), "unknown": True})
            continue
        row = dict(record)
        row.pop("_fixtures", None)
        planned = context_rows.get(player_id, {})
        element = elements.get(player_id, {})
        row.update(
            {
                "purchase_price": planned.get("purchase_price"),
                "purchase_source": planned.get("purchase_source"),
                "selling_price": planned.get("selling_price"),
                "new": bool(planned.get("new")),
                "locked": player_id in locked,
                "banned": player_id in banned,
                "override": overrides.get(player_id),
                "bench_use": bench_use.get(player_id),
                **element,
                "price_pressure": my_team.price_pressure(
                    {**element, "selected_by_percent": record.get("selected_by_percent")},
                    total_players,
                ),
                "selling_effect": my_team.selling_effect(
                    planned.get("purchase_price"), record.get("price")
                )
                if plan_context
                else None,
            }
        )
        squad.append(row)

    manager_name = " ".join(
        str(manager.get(field) or "").strip() for field in ("player_first_name", "player_last_name")
    ).strip()
    result = {
        "entry_id": entry_id,
        "team_name": manager.get("name"),
        "manager_name": manager_name or None,
        "gameweek": prediction_gameweek,
        "picks_gameweek": picks_gameweek,
        "transfers_open": bool(is_open),
        "next_open_gameweek": client.next_gameweek(),
        "frozen": bool(predictions.attrs.get("frozen", False)),
        "forecast": _forecast_meta(predictions),
        "edited": draft is not None,
        "chip": chip,
        "squad": squad,
        "published_picks": published_picks,
        "working_picks": my_team.normalize_picks(working_picks, official) if structure_ok else working_picks,
        "validation": validation,
        "changes": changes,
        "rating": rating,
        "published_rating": published_rating,
        "expected": expected,
        "advice": advice,
        "captaincy": captaincy,
        "transfers": suggestions,
        "clubs_over_limit": clubs_over_limit(working_records),
        "locked_ids": locked,
        "banned_ids": banned,
        "overrides_applied": sorted(
            player_id
            for player_id, value in overrides.items()
            if value.get("availability") is not None or value.get("xp_pct") is not None
        ),
        "notes": notes,
        "caveats": PLAN_CAVEATS,
        "benchmark": "The AI benchmark is the AI's own squad from its unadjusted projections.",
    }
    if plan_context:
        result.update(
            {
                key: plan_context[key]
                for key in (
                    "bank",
                    "published_bank",
                    "squad_value",
                    "budget",
                    "free_transfers",
                    "free_transfers_used",
                    "free_transfers_remaining",
                    "chips",
                )
            }
        )
    else:
        result.update(
            {
                "bank": manager.get("last_deadline_bank"),
                "published_bank": manager.get("last_deadline_bank"),
                "free_transfers": None,
                "chips": None,
            }
        )
    return _json(result)


@router.post(
    "/managers/{entry_id}/team",
    summary="A manager's working squad: rating, checks, lineup and captaincy advice",
)
async def admin_working_team(
    entry_id: int, request: TeamRequest = Body(default_factory=TeamRequest)
):
    return await _run(_working_team, entry_id, request)


class OptimizeRequest(BaseModel):
    entry_id: int = Field(..., ge=1)
    gameweek: Optional[int] = Field(None, ge=1, le=38)
    mode: Literal["transfers", "wildcard", "freehit"] = "transfers"
    horizon: Optional[int] = Field(None, ge=1, le=5)
    free_transfers: Optional[int] = Field(None, ge=0, le=5)
    bank: Optional[float] = Field(None, ge=0, le=100)
    max_transfers: Optional[int] = Field(None, ge=0, le=15)
    draft: Optional[Draft] = None
    overrides: Dict[int, PlayerOverride] = Field(default_factory=dict)


HORIZONS = {"wildcard": 5, "freehit": 1}


def _horizon(
    scout: Any, gameweeks: List[int], overrides: Mapping[int, Mapping[str, Any]]
) -> tuple:
    """Projected points per player and gameweek, with the manager's overrides."""

    def predict(gameweek: int) -> pd.DataFrame:
        return scout.get_official_predictions(
            gameweek, collect_results=False, allow_stale=False
        )

    points, identity, notes = optimizer.horizon_points(predict, gameweeks)
    factors = points.attrs.get("factors")
    if any(
        value.get("availability") is not None or value.get("xp_pct") is not None
        for value in overrides.values()
    ):
        points, override_notes = my_team.apply_overrides_frame(
            points,
            factors,
            overrides,
            raw_lookup=lambda gameweek, ids: _raw_projections(scout, gameweek, list(ids)),
        )
        notes = notes + override_notes + [
            "Your own availability and xP settings apply to every gameweek planned."
        ]
    return points, identity, factors, notes


def _optimizer_players(
    plan_context: Mapping[str, Any],
    points: pd.DataFrame,
    identity: pd.DataFrame,
    gameweeks: List[int],
    official: Mapping[int, Mapping[str, Any]],
) -> pd.DataFrame:
    """Candidates for the solver: current squad at selling prices, others at price."""
    squad_by_id = {row["id"]: row for row in plan_context["squad"]}
    ids = sorted(set(points.index) | set(squad_by_id))
    rows = []
    for player_id in ids:
        details = official.get(player_id, {})
        known = identity.loc[player_id] if player_id in identity.index else None
        element_type = insights.position_code(
            known["element_type"] if known is not None else details.get("element_type")
        )
        if element_type not in optimizer.SQUAD_QUOTAS:
            continue
        in_squad = player_id in squad_by_id
        price = details.get("price")
        if price is None and not in_squad:
            continue
        rows.append(
            {
                "id": player_id,
                "web_name": details.get("web_name") or (known["web_name"] if known is not None else None),
                "team_name": details.get("team_name") or (known["team_name"] if known is not None else None),
                "element_type": element_type,
                "price": price,
                "cost": squad_by_id[player_id]["selling_price"] if in_squad else price,
                "buyable": bool(currently_available(details)) if details else False,
                "availability_factor": None,
                "news": details.get("news") or "",
                **{
                    gameweek: float(points.at[player_id, gameweek])
                    if player_id in points.index
                    else 0.0
                    for gameweek in gameweeks
                },
            }
        )
    players = pd.DataFrame(rows)
    players["cost"] = players["cost"].fillna(players["price"]).fillna(0.0)
    return players


def _optimize(request: OptimizeRequest) -> Dict[str, Any]:
    scout = _scout()
    overrides = _overrides(request.overrides)
    plan_context = _manager_plan_context(request.entry_id, request.gameweek, request.draft)
    start = plan_context["gameweek"]
    horizon = HORIZONS.get(request.mode, request.horizon or 3)
    gameweeks = [gameweek for gameweek in range(start, start + horizon) if gameweek <= 38]
    points, identity, _, notes = _horizon(scout, gameweeks, overrides)
    players = _optimizer_players(plan_context, points, identity, gameweeks, _official_players(scout))
    current_ids = [row["id"] for row in plan_context["squad"]]
    free_transfers = request.free_transfers
    if free_transfers is None:
        free_transfers = plan_context["free_transfers_remaining"]
    bank = plan_context["bank"] if request.bank is None else float(request.bank)
    result = optimizer.plan(
        players,
        gameweeks,
        mode=request.mode,
        current_ids=current_ids,
        bank=bank,
        free_transfers=free_transfers,
        max_transfers=request.max_transfers,
        locked_ids=[player_id for player_id, value in overrides.items() if value["lock"]],
        banned_ids=[player_id for player_id, value in overrides.items() if value["ban"]],
    )
    return _json(
        {
            **result,
            "entry_id": request.entry_id,
            "start_gameweek": start,
            "horizon": len(gameweeks),
            "from_working_squad": request.draft is not None,
            "free_transfers_estimate": plan_context["free_transfers"].get("free_transfers"),
            "notes": notes + plan_context["caveats"],
        }
    )


@router.post("/optimize", summary="Plan transfers, a Wildcard or a Free Hit for a manager")
async def admin_optimize(request: OptimizeRequest):
    return await _run(_optimize, request)

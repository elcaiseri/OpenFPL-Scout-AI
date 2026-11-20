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
from typing import Any, Callable, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from src import insights
from src import auth as auth_module
from src.auth import verify_api_key
from src.features import MODEL_FEATURES
from src.logger import get_logger
from src.observability import LogBuffer, RequestMetrics, process_snapshot
from src.official_fpl import OfficialFPLAPIError, OfficialFPLNotFoundError

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

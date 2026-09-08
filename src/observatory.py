"""Pure comparison analytics for the owner observatory. Never infer missing results."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

POSITIONS = ("GK", "DEF", "MID", "FWD")


def numeric(value: Any):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def total(values):
    """A complete total or unknown; missing values are never imputed as zero."""
    values = list(values)
    return sum(values) if values and all(v is not None for v in values) else None


def point_metrics(players):
    pairs = [(numeric(p.get("expected_points")), numeric(p.get("actual_points"))) for p in players]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if not pairs:
        return dict.fromkeys(("mae", "rmse", "bias", "within_two_pct", "predicted_mean", "actual_mean", "predicted_total", "actual_total", "r2", "rank_correlation"), None) | {"count": 0}
    predicted, actual = np.array(pairs, dtype=float).T
    errors = predicted - actual
    spread = float(np.sum((actual - actual.mean()) ** 2))
    rank_correlation = None
    if len(pairs) > 1 and np.std(predicted) > 0 and np.std(actual) > 0:
        rank_correlation = float(np.corrcoef(pd.Series(predicted).rank(), pd.Series(actual).rank())[0, 1])
    return {
        "count": len(pairs), "mae": float(np.abs(errors).mean()),
        "rmse": float(np.sqrt(np.mean(errors ** 2))), "bias": float(errors.mean()),
        "within_two_pct": float(np.mean(np.abs(errors) <= 2) * 100),
        "predicted_mean": float(predicted.mean()), "actual_mean": float(actual.mean()),
        "predicted_total": float(predicted.sum()), "actual_total": float(actual.sum()),
        "r2": 1 - float(np.sum(errors ** 2)) / spread if spread else None,
        "rank_correlation": rank_correlation,
    }


def ranked(players):
    return sorted(players, key=lambda p: (-(p.get("selection_score") if p.get("selection_score") is not None else p.get("expected_points") or 0), p["id"]))


def selection_metrics(players, k=10):
    matched = [p for p in players if p.get("actual_points") is not None]
    chosen = ranked(matched)[:k]
    best = sorted(matched, key=lambda p: (-p["actual_points"], p["id"]))[:k]
    hit_ids = {p["id"] for p in best}
    dcg = sum(max(0, p["actual_points"]) / math.log2(i + 2) for i, p in enumerate(chosen))
    ideal = sum(max(0, p["actual_points"]) / math.log2(i + 2) for i, p in enumerate(best))
    return {
        "count": len(chosen), "pool": len(matched),
        "top10_overlap_pct": 100 * sum(p["id"] in hit_ids for p in chosen) / len(chosen) if chosen else None,
        "haul_rate_pct": 100 * sum(p["actual_points"] >= 6 for p in chosen) / len(chosen) if chosen else None,
        "ndcg": dcg / ideal if ideal else None,
        "chosen_actual": total(p["actual_points"] for p in chosen),
        "best_actual": total(p["actual_points"] for p in best),
        "our_top": chosen, "actual_top": best,
    }


def best_xi(players, field):
    groups = {position: [] for position in POSITIONS}
    for p in players:
        if p.get("position") in groups and numeric(p.get(field)) is not None:
            groups[p["position"]].append(p)
    for group in groups.values():
        group.sort(key=lambda p: (-p[field], p["id"]))
    choices = []
    for defenders in range(3, 6):
        for midfielders in range(2, 6):
            forwards = 10 - defenders - midfielders
            if not 1 <= forwards <= 3:
                continue
            counts = dict(zip(POSITIONS, (1, defenders, midfielders, forwards)))
            if any(len(groups[p]) < n for p, n in counts.items()):
                continue
            xi = [p for position, n in counts.items() for p in groups[position][:n]]
            choices.append((sum(p[field] for p in xi), xi))
    return max(choices, key=lambda choice: choice[0])[1] if choices else []


def squad_analysis(players):
    squad = [p for p in players if p.get("in_squad")]
    complete_squad = len(squad) == len({p["id"] for p in squad}) == 15 and all(
        sum(p.get("position") == position for p in squad) == count
        for position, count in zip(POSITIONS, (2, 5, 5, 3))
    )
    if not complete_squad:
        squad = []
    xi = best_xi(squad, "selection_score")
    captain = ranked(xi)[0] if xi else None
    benchmark_actual = None
    if xi and all(p.get("actual_points") is not None for p in xi):
        benchmark_actual = sum(p["actual_points"] for p in xi) + captain["actual_points"]
    hindsight = best_xi(squad, "actual_points") if squad and all(p.get("actual_points") is not None for p in squad) else []
    best_actual = sum(p["actual_points"] for p in hindsight) + max(p["actual_points"] for p in hindsight) if hindsight else None
    projected = total(p.get("expected_points") for p in xi)
    projected = projected + captain["expected_points"] if projected is not None and captain.get("expected_points") is not None else None
    xi_ids = {p["id"] for p in xi}
    return {
        "xi": [{**p, "is_captain": p["id"] == captain["id"]} for p in xi],
        "bench": [p for p in squad if p["id"] not in xi_ids],
        "predicted_points": projected, "actual_points": benchmark_actual,
        "hindsight_points": best_actual,
        "selection_gap": best_actual - benchmark_actual if best_actual is not None and benchmark_actual is not None else None,
        "note": "Derived from the saved 15-player shortlist: highest-ranked legal XI, captain doubled, no autosubs or chips. Hindsight optimizes the same shortlist using actual points. Budget-free benchmark.",
    }


def calibration(players):
    bins = [("<2", -math.inf, 2), ("2–4", 2, 4), ("4–6", 4, 6), ("6–8", 6, 8), ("8+", 8, math.inf)]
    return [{"band": label, **point_metrics([p for p in players if p.get("expected_points") is not None and low <= p["expected_points"] < high])} for label, low, high in bins]


def grouped_metrics(players, field):
    groups = defaultdict(list)
    for p in players:
        groups[str(p.get(field) or "Unknown")].append(p)
    return [{"name": name, **point_metrics(group)} for name, group in sorted(groups.items())]


def model_comparison(players):
    names = sorted({name for p in players for name in p.get("model_predictions", {})})
    output = [{"name": "ensemble", **point_metrics(players)}]
    for name in names:
        values = [{"expected_points": p.get("model_predictions", {}).get(name), "actual_points": p.get("actual_points")} for p in players]
        output.append({"name": name, **point_metrics(values)})
    return output


def scout_analysis(week):
    """Audit the saved Scout output, without reconstructing or rescoring picks."""
    squad = week["squad"]
    players = [p for p in week["players"] if p.get("in_squad")] if squad["matches_forecast"] else []
    expected = total(p.get("expected_points") for p in players)
    actual = total(p.get("actual_points") for p in players)
    return {
        "players": players,
        "count": len(players),
        "matched_actuals": sum(p.get("actual_points") is not None for p in players),
        "expected_points": expected,
        "actual_points": actual,
        "error": expected - actual if expected is not None and actual is not None else None,
        "metrics": point_metrics(players),
        "positions": grouped_metrics(players, "position"),
        "captain": next((p for p in players if p.get("role") == "captain"), None),
        "vice": next((p for p in players if p.get("role") == "vice"), None),
        "archive_state": "matching" if squad["matches_forecast"] else "mismatched" if squad["count"] else "missing",
    }


def scout_season_analysis(weeks, verified=False):
    completed = [
        w for w in weeks
        if w["result_state"] == "final" and w["squad"]["matches_forecast"]
        and (not verified or (w["eligible"] and w["squad"]["eligible"]))
    ]
    players = [p for w in completed for p in w["players"] if p.get("in_squad")]
    timeline = []
    for week in completed:
        picks = [p for p in week["players"] if p.get("in_squad")]
        timeline.append({
            "gameweek": week["gameweek"], "forecast_state": week["forecast_state"],
            "selected_count": len(picks),
            "is_points_forecast": week.get("is_points_forecast", True),
            **point_metrics(picks),
        })
    return {
        "metrics": point_metrics(players), "timeline": timeline,
        "positions": grouped_metrics(players, "position"),
    }


def season_analysis(weeks, verified=False):
    completed = [w for w in weeks if w["result_state"] == "final" and (not verified or w["eligible"])]
    players = [p for w in completed for p in w["players"]]
    by_player = defaultdict(list)
    for w in completed:
        for p in w["players"]:
            by_player[p["id"]].append({**p, "gameweek": w["gameweek"]})
    leaders = []
    for player_id, history in by_player.items():
        last = history[-1]
        leaders.append({"id": player_id, "name": last["name"], "team": last["team"], "position": last["position"], "gameweeks": len(history), **point_metrics(history), "scored_points": sum(p["actual_points"] for p in history if p.get("actual_points") is not None)})
    leaders.sort(key=lambda p: (-(p["actual_total"] or 0), p["id"]))
    ranked_weeks = [w for w in completed if w["players"] and any(p.get("actual_points") is not None for p in w["players"])]
    return {
        "metrics": point_metrics(players), "calibration": calibration(players),
        "scout": scout_season_analysis(weeks, verified),
        "positions": grouped_metrics(players, "position"), "clubs": grouped_metrics(players, "team"),
        "models": model_comparison(players), "players": leaders,
        "selection": [{"gameweek": w["gameweek"], **{k: v for k, v in selection_metrics(w["players"]).items() if k not in ("our_top", "actual_top")}} for w in ranked_weeks],
        "timeline": [{"gameweek": w["gameweek"], "forecast_state": w["forecast_state"], "result_state": w["result_state"], "is_points_forecast": w.get("is_points_forecast", True), **point_metrics(w["players"])} for w in completed if w["prediction_count"]],
        "decisions": [{"gameweek": w["gameweek"], "forecast_state": w["forecast_state"], **{k: v for k, v in w["analysis"]["squad"].items() if k not in ("xi", "bench")}, "official_average": w["analysis"]["average_manager_score"]} for w in completed if w["prediction_count"] and (not verified or w["squad"]["eligible"])],
    }


def gameweek_analysis(week, event, fixtures, teams):
    players = week["players"]
    matched = [p for p in players if p.get("error") is not None]
    totals = []
    for stat in ("goals_scored", "assists", "clean_sheets", "bonus", "saves", "yellow_cards", "red_cards"):
        values = [numeric(p.get(stat)) for p in week.get("official_stats", {}).values()]
        totals.append({"name": "player_clean_sheets" if stat == "clean_sheets" else stat, "actual": total(values)})
    match_rows = []
    for f in fixtures:
        if f.get("event") != week["gameweek"]:
            continue
        match_rows.append({
            "id": f.get("id"), "home": teams.get(f.get("team_h"), str(f.get("team_h"))),
            "away": teams.get(f.get("team_a"), str(f.get("team_a"))),
            "home_score": f.get("team_h_score"), "away_score": f.get("team_a_score"),
            "kickoff_time": f.get("kickoff_time"), "finished": bool(f.get("finished")),
            "started": bool(f.get("started")), "home_difficulty": f.get("team_h_difficulty"),
            "away_difficulty": f.get("team_a_difficulty"),
        })
    return {
        "selection": selection_metrics(players),
        "scout": scout_analysis(week),
        "squad": squad_analysis(players if week["squad"]["matches_forecast"] else []),
        "calibration": calibration(players), "models": model_comparison(players),
        "clubs": grouped_metrics(players, "team"), "fixtures": match_rows,
        "biggest_under": sorted((p for p in matched if p["error"] < 0), key=lambda p: p["error"])[:5],
        "biggest_over": sorted((p for p in matched if p["error"] > 0), key=lambda p: -p["error"])[:5],
        "official_totals": totals,
        "average_manager_score": numeric(event.get("average_entry_score")) if week["result_state"] != "awaiting-results" else None,
        "highest_manager_score": numeric(event.get("highest_score")),
        "most_captained": event.get("most_captained"),
        "finished_fixtures": sum(f["finished"] for f in match_rows), "fixture_count": len(match_rows),
    }

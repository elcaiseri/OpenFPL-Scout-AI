"""Explanations, selection traces and accuracy checks for the owner dashboard.

Everything here is read-only analysis of forecasts the service already
makes: it never changes a cached or archived forecast.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.features import HISTORY_FEATURES
from src.team_rating import best_lineup

TEAM_SELECTION: Dict[int, int] = {1: 2, 2: 5, 3: 5, 4: 3}
POSITION_NAMES = {1: "Goalkeeper", 2: "Defender", 3: "Midfielder", 4: "Forward"}
POSITION_SHORT = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
POSITION_CODES = {
    **{key: key for key in POSITION_NAMES},
    **{name: key for key, name in POSITION_NAMES.items()},
    **{name: key for key, name in POSITION_SHORT.items()},
}

FEATURE_LABELS = {
    "now_cost": "Price",
    "selected_by_percent": "Ownership %",
    "minutes": "Minutes",
    "total_shots": "Shots",
    "shots_on_target": "Shots on target",
    "shots_in_box": "Shots in box",
    "expected_goals": "xG",
    "non_penalty_expected_goals": "npxG",
    "goals": "Goals",
    "non_penalty_goals": "Non-penalty goals",
    "chances_created": "Chances created",
    "expected_assists": "xA",
    "assists": "Assists",
    "expected_goals_conceded": "xG conceded",
    "goals_conceded": "Goals conceded",
    "expected_clean_sheet": "xCS",
    "clean_sheet": "Clean sheets",
    "clearances": "Clearances",
    "shot_blocks": "Blocks",
    "interceptions": "Interceptions",
    "recoveries": "Recoveries",
    "tackles": "Tackles",
    "clearances_blocks_interceptions": "CBI",
    "defensive_contribution": "Defensive contribution",
    "expected_goal_involvements": "xGI",
    "non_penalty_expected_goal_involvements": "npxGI",
    "expected_points": "FPL xP",
    "PvsxP": "Points vs xP",
    "touches": "Touches",
    "touches_opp_box": "Touches in opp. box",
    "carries_final_third": "Carries into final third",
    "carries_penalty_area": "Carries into box",
}

FEATURE_GROUPS: Dict[str, List[str]] = {
    "Attack": [
        "total_shots",
        "shots_on_target",
        "shots_in_box",
        "expected_goals",
        "non_penalty_expected_goals",
        "goals",
        "non_penalty_goals",
        "touches_opp_box",
    ],
    "Creativity": [
        "chances_created",
        "expected_assists",
        "assists",
        "expected_goal_involvements",
        "non_penalty_expected_goal_involvements",
        "carries_final_third",
        "carries_penalty_area",
    ],
    "Defence": [
        "expected_goals_conceded",
        "goals_conceded",
        "expected_clean_sheet",
        "clean_sheet",
        "clearances",
        "shot_blocks",
        "interceptions",
        "recoveries",
        "tackles",
        "clearances_blocks_interceptions",
        "defensive_contribution",
    ],
    "Minutes": ["minutes", "touches"],
    "Market": ["now_cost", "selected_by_percent"],
    "FPL form": ["expected_points", "PvsxP"],
}
FEATURE_GROUP = {
    feature: group for group, features in FEATURE_GROUPS.items() for feature in features
}

# The 5-match averages shown as table columns (the rest are in the drawer).
TABLE_FEATURES = [
    "minutes",
    "expected_goals",
    "non_penalty_expected_goals",
    "expected_assists",
    "expected_goal_involvements",
    "total_shots",
    "shots_on_target",
    "chances_created",
    "goals",
    "assists",
    "expected_goals_conceded",
    "clean_sheet",
    "defensive_contribution",
    "recoveries",
    "tackles",
    "expected_points",
    "PvsxP",
]


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def clean(value: Any, digits: Optional[int] = None) -> Any:
    """JSON-safe scalar: NaN/inf become None, numpy types become Python."""
    if value is None:
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return round(number, digits) if digits is not None else number
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _number(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def position_code(value: Any) -> int:
    if isinstance(value, str):
        value = value.strip()
    try:
        if value in POSITION_CODES:
            return POSITION_CODES[value]
        return int(value)
    except (TypeError, ValueError):
        return 0


def model_points_by_id(inference: Mapping[str, Any]) -> Dict[int, Dict[str, float]]:
    """``{player_id: {model: points}}`` from forecast diagnostics, if present."""
    payload = (inference or {}).get("model_predictions") or {}
    ids = payload.get("ids") or []
    points = payload.get("points") or {}
    result: Dict[int, Dict[str, float]] = {}
    for model, values in points.items():
        for player_id, value in zip(ids, values):
            result.setdefault(int(player_id), {})[str(model)] = value
    return result


def _pearson(left: pd.Series, right: pd.Series) -> Optional[float]:
    if len(left) < 3 or left.std() == 0 or right.std() == 0:
        return None
    return clean(float(np.corrcoef(left, right)[0, 1]), 4)


def _spearman(left: pd.Series, right: pd.Series) -> Optional[float]:
    return _pearson(left.rank(method="average"), right.rank(method="average"))


# ----------------------------------------------------------------------
# Squad selection, with the reason behind every decision
# ----------------------------------------------------------------------


def select_with_trace(
    predictions: pd.DataFrame,
    *,
    team_selection: Mapping[int, int] = TEAM_SELECTION,
    max_per_club: int = 3,
    locks: Iterable[int] = (),
    excludes: Iterable[int] = (),
    require_available: Optional[bool] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Select the squad exactly as ``FPLScout.select_optimal_team`` does,
    and explain each player's outcome.

    Players are taken in descending projection while their position has room
    and their club has fewer than ``max_per_club``. ``locks`` are taken
    first (where quotas and the club limit allow) and ``excludes`` never;
    without either the squad equals the live service's squad.
    """
    if predictions.empty:
        raise ValueError("Cannot select a team from empty predictions")
    strategy = (predictions.attrs.get("inference") or {}).get("strategy")
    if require_available is None:
        require_available = strategy == "ownership-cold-start"
    player_key = "id" if "id" in predictions.columns else "web_name"
    unique = predictions.drop_duplicates(player_key, keep="first").copy()
    unique["_position"] = unique["element_type"].map(position_code)
    unique["_score"] = pd.to_numeric(unique["expected_points"], errors="coerce")
    if "availability_factor" in unique.columns:
        unique["_availability"] = pd.to_numeric(
            unique["availability_factor"], errors="coerce"
        ).fillna(0.0 if require_available else 1.0)
    else:
        unique["_availability"] = 1.0
    locks = [int(value) for value in locks]
    excluded = {int(value) for value in excludes}

    ranked = unique.sort_values("_score", ascending=False, kind="stable")
    overall_rank = {}
    position_rank = {}
    position_seen: Dict[int, int] = {}
    for order, (_, player) in enumerate(ranked.iterrows(), start=1):
        key = player[player_key]
        overall_rank[key] = order
        position = int(player["_position"]) if pd.notna(player["_position"]) else 0
        position_seen[position] = position_seen.get(position, 0) + 1
        position_rank[key] = position_seen[position]

    decisions: Dict[Any, Dict[str, Any]] = {}
    eligible_mask = (
        unique["_position"].isin(list(team_selection))
        & unique["_score"].notna()
        & unique["_availability"].gt(0)
        & unique["team_name"].notna()
    )
    for _, player in unique.iterrows():
        key = player[player_key]
        base = {
            "rank_overall": overall_rank.get(key),
            "rank_in_position": position_rank.get(key),
        }
        if player_key == "id" and int(key) in excluded:
            decisions[key] = {**base, "status": "excluded", "reason": "Excluded by you"}
        elif pd.isna(player["_score"]):
            decisions[key] = {**base, "status": "no-projection", "reason": "No projection"}
        elif player["_availability"] <= 0:
            decisions[key] = {
                **base,
                "status": "unavailable",
                "reason": "Not available (injured, suspended, left the league or 0% chance)",
            }
        elif pd.isna(player["team_name"]) or player["_position"] not in team_selection:
            decisions[key] = {**base, "status": "ineligible", "reason": "No club or position"}

    candidates = unique.loc[eligible_mask].copy()
    if player_key == "id":
        candidates = candidates.loc[
            ~pd.to_numeric(candidates["id"], errors="coerce").isin(excluded)
        ]
    for position, count in team_selection.items():
        available = int(candidates["_position"].eq(position).sum())
        if available < count:
            raise ValueError(f"Need {count} position-{position} players, found {available}")

    selected: List[Any] = []
    selected_keys: List[Any] = []
    position_counts = {position: 0 for position in team_selection}
    club_members: Dict[str, List[str]] = {}

    def take(index: Any, player: pd.Series, status: str, reason: str) -> None:
        selected.append(index)
        selected_keys.append(player[player_key])
        position = int(player["_position"])
        position_counts[position] += 1
        club_members.setdefault(str(player["team_name"]), []).append(
            str(player.get("web_name"))
        )
        decisions[player[player_key]] = {
            "rank_overall": overall_rank.get(player[player_key]),
            "rank_in_position": position_rank.get(player[player_key]),
            "status": status,
            "reason": reason,
        }

    candidate_ranked = candidates.sort_values("_score", ascending=False, kind="stable")
    if locks and player_key == "id":
        locked_rows = candidate_ranked.loc[
            pd.to_numeric(candidate_ranked["id"], errors="coerce").isin(locks)
        ]
        for index, player in locked_rows.iterrows():
            position = int(player["_position"])
            club = str(player["team_name"])
            if position_counts[position] >= team_selection[position]:
                decisions[player[player_key]] = {
                    "rank_overall": overall_rank.get(player[player_key]),
                    "rank_in_position": position_rank.get(player[player_key]),
                    "status": "lock-rejected",
                    "reason": f"Lock ignored: {POSITION_NAMES[position]} quota already full of locks",
                }
                continue
            if len(club_members.get(club, [])) >= max_per_club:
                decisions[player[player_key]] = {
                    "rank_overall": overall_rank.get(player[player_key]),
                    "rank_in_position": position_rank.get(player[player_key]),
                    "status": "lock-rejected",
                    "reason": f"Lock ignored: {club} already has {max_per_club} locked players",
                }
                continue
            take(index, player, "locked", "Locked in by you")
        for player_id in locks:
            if player_id not in set(pd.to_numeric(candidates["id"], errors="coerce")):
                decisions.setdefault(
                    player_id,
                    {"status": "lock-rejected", "reason": "Lock ignored: not eligible"},
                )

    total = sum(team_selection.values())
    for index, player in candidate_ranked.iterrows():
        if index in selected:
            continue
        key = player[player_key]
        position = int(player["_position"])
        club = str(player["team_name"])
        if len(selected) >= total or position_counts[position] >= team_selection[position]:
            decisions.setdefault(
                key,
                {
                    "rank_overall": overall_rank.get(key),
                    "rank_in_position": position_rank.get(key),
                    "status": "position-full",
                    "reason": (
                        f"All {team_selection[position]} {POSITION_NAMES[position]} "
                        "places went to higher projections"
                    ),
                },
            )
            continue
        if len(club_members.get(club, [])) >= max_per_club:
            decisions.setdefault(
                key,
                {
                    "rank_overall": overall_rank.get(key),
                    "rank_in_position": position_rank.get(key),
                    "status": "club-limit",
                    "reason": (
                        f"{club} already had {max_per_club} players in the squad: "
                        + ", ".join(club_members[club])
                    ),
                    "blocking_club": club,
                    "club_players": list(club_members[club]),
                },
            )
            continue
        take(
            index,
            player,
            "selected",
            f"#{position_seen_rank(position_counts, position)} of "
            f"{team_selection[position]} {POSITION_NAMES[position]} places",
        )

    if any(position_counts[p] != c for p, c in team_selection.items()):
        raise ValueError("Could not construct a Scout squad within the per-team player limit")

    internal = ["_position", "_score", "_availability"]
    # Mirrors FPLScout._select_budget_free_squad, including its sort call, so
    # ties order (and the captain) exactly as the live squad does.
    team = (
        unique.loc[selected]
        .drop(columns=internal, errors="ignore")
        .sort_values("expected_points", ascending=False)
        .reset_index(drop=True)
    )
    team["role"] = ""
    team.loc[0, "role"] = "captain"
    team.loc[1, "role"] = "vice"
    team["element_type"] = team["element_type"].map(
        lambda value: POSITION_NAMES.get(position_code(value), value)
    )

    cutoffs: Dict[str, Any] = {}
    for position, count in team_selection.items():
        chosen = candidate_ranked.loc[
            [index for index in selected if candidate_ranked.loc[index, "_position"] == position]
        ].sort_values("_score", ascending=False, kind="stable")
        left_out = candidate_ranked.loc[
            candidate_ranked["_position"].eq(position)
            & ~candidate_ranked.index.isin(selected)
        ]
        first_out = None
        if len(left_out):
            first = left_out.iloc[0]
            decision = decisions.get(first[player_key], {})
            first_out = {
                **_brief(first),
                "status": decision.get("status"),
                "reason": decision.get("reason"),
                "blocking_club": decision.get("blocking_club"),
            }
        cutoffs[POSITION_SHORT[position]] = {
            "places": count,
            "last_selected": _brief(chosen.iloc[-1]) if len(chosen) else None,
            "first_left_out": first_out,
        }

    near_misses = []
    for index, player in candidate_ranked.iterrows():
        if index in selected:
            continue
        decision = decisions.get(player[player_key], {})
        near_misses.append({**_brief(player), **decision})
        if len(near_misses) >= 12:
            break

    # Players the club limit kept out although they out-project the last pick
    # in their position: the rule, not the projection, decided.
    last_in = {
        position: min(
            (
                float(candidate_ranked.loc[index, "_score"])
                for index in selected
                if candidate_ranked.loc[index, "_position"] == position
            ),
            default=None,
        )
        for position in team_selection
    }
    club_limited = []
    for index, player in candidate_ranked.iterrows():
        decision = decisions.get(player[player_key], {})
        threshold = last_in.get(int(player["_position"]))
        if (
            decision.get("status") == "club-limit"
            and threshold is not None
            and float(player["_score"]) > threshold
        ):
            club_limited.append({**_brief(player), **decision})

    trace = {
        "max_per_club": max_per_club,
        "club_limited": club_limited,
        "team_selection": {POSITION_SHORT[key]: value for key, value in team_selection.items()},
        "squad_ids": [clean(key) for key in selected_keys],
        "decisions": {clean(key): value for key, value in decisions.items()},
        "cutoffs": cutoffs,
        "near_misses": near_misses,
        "club_counts": {club: len(names) for club, names in sorted(club_members.items())},
    }
    return team, trace


def position_seen_rank(position_counts: Mapping[int, int], position: int) -> int:
    return int(position_counts[position]) + 1


def _brief(player: pd.Series) -> Dict[str, Any]:
    return {
        "id": clean(player.get("id")),
        "web_name": clean(player.get("web_name")),
        "team_name": clean(player.get("team_name")),
        "position": POSITION_SHORT.get(position_code(player.get("element_type")), "?"),
        "expected_points": clean(_number(player.get("expected_points")), 2),
    }


def captaincy_reasoning(team: pd.DataFrame) -> Dict[str, Any]:
    """Why the captain and vice were chosen: projection margins."""
    if len(team) < 3:
        return {}
    ordered = team.sort_values("expected_points", ascending=False, kind="stable")
    first, second, third = (ordered.iloc[index] for index in range(3))
    first_points = _number(first["expected_points"])
    second_points = _number(second["expected_points"])
    third_points = _number(third["expected_points"])
    return {
        "captain": _brief(first),
        "vice": _brief(second),
        "captain_margin": round(first_points - second_points, 2),
        "vice_margin": round(second_points - third_points, 2),
        "summary": (
            f"{first.get('web_name')} has the squad's highest projection "
            f"({first_points:.2f}), {first_points - second_points:.2f} ahead of "
            f"{second.get('web_name')} ({second_points:.2f}), who is vice-captain."
        ),
    }


def lineup_summary(team_records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The best legal XI from a 15-man squad, its formation and total."""
    lineup, total = best_lineup(team_records)
    counts = {position: 0 for position in (2, 3, 4)}
    for player in lineup:
        position = position_code(player.get("element_type", player.get("position")))
        if position in counts:
            counts[position] += 1
    captain = max(lineup, key=lambda player: _number(player.get("expected_points")))
    return {
        "starting_ids": [clean(player.get("id")) for player in lineup],
        "formation": f"{counts[2]}-{counts[3]}-{counts[4]}",
        "starting_points": round(total, 2),
        "captain_id": clean(captain.get("id")),
        "projected_total": round(total + _number(captain.get("expected_points")), 2),
    }


# ----------------------------------------------------------------------
# The full prediction table
# ----------------------------------------------------------------------


def _fixtures_of(player_row: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    fixtures = (player_row or {}).get("_fixtures")
    if not isinstance(fixtures, (list, tuple)):
        return []
    return [
        {
            "opponent": clean(fixture.get("opponent_team_name")),
            "home": clean(fixture.get("was_home")),
            "difficulty": clean(fixture.get("difficulty")),
            "kickoff_time": clean(fixture.get("kickoff_time")),
        }
        for fixture in fixtures
        if isinstance(fixture, Mapping)
    ]


def prediction_table(
    predictions: pd.DataFrame,
    *,
    inputs_players: Optional[pd.DataFrame] = None,
    official_players: Optional[Mapping[int, Mapping[str, Any]]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    team: Optional[pd.DataFrame] = None,
) -> List[Dict[str, Any]]:
    """One row per player: projection, model view, inputs, status, selection."""
    model_points = model_points_by_id(predictions.attrs.get("inference", {}))
    inputs_by_id: Dict[int, Dict[str, Any]] = {}
    if inputs_players is not None and not inputs_players.empty:
        for record in inputs_players.to_dict(orient="records"):
            try:
                inputs_by_id[int(record["id"])] = record
            except (KeyError, TypeError, ValueError):
                continue
    official_players = official_players or {}
    decisions = (trace or {}).get("decisions", {})
    roles: Dict[int, str] = {}
    if team is not None and "id" in team.columns:
        for record in team.to_dict(orient="records"):
            roles[int(record["id"])] = str(record.get("role") or "")

    rows = []
    for record in predictions.drop_duplicates("id").to_dict(orient="records"):
        player_id = int(record["id"])
        official = official_players.get(player_id, {})
        inputs = inputs_by_id.get(player_id)
        models = model_points.get(player_id, {})
        model_values = [value for value in models.values() if value is not None]
        expected = _number(record.get("expected_points"))
        price = _number(official.get("price"), None)
        if price is None and inputs is not None:
            price = _number(inputs.get("now_cost"), None)
        decision = decisions.get(player_id) or {}
        position = position_code(record.get("element_type"))
        row = {
            "id": player_id,
            "web_name": clean(record.get("web_name")),
            "team_name": clean(record.get("team_name")),
            "team_id": clean(official.get("team_id")),
            "position": POSITION_SHORT.get(position, "?"),
            "element_type": position,
            "opponent": clean(record.get("opponent_team_name")),
            "was_home": clean(record.get("was_home")),
            "fixture_count": clean(record.get("fixture_count")),
            "fixtures": _fixtures_of(inputs),
            "expected_points": clean(expected, 3),
            "availability_factor": clean(_number(record.get("availability_factor"), None), 3),
            "models": {name: clean(value, 3) for name, value in models.items()},
            "model_spread": clean(float(np.std(model_values)), 3)
            if len(model_values) > 1
            else None,
            "price": clean(price, 1),
            "value": clean(expected / price, 3) if price else None,
            "selected_by_percent": clean(official.get("selected_by_percent")),
            "status": clean(official.get("status")),
            "news": clean(official.get("news")) or "",
            "chance_of_playing_next_round": clean(
                official.get("chance_of_playing_next_round")
            ),
            "total_points": clean(official.get("total_points")),
            "season": {
                "minutes": clean(official.get("minutes")),
                "goals": clean(official.get("goals")),
                "assists": clean(official.get("assists")),
                "expected_goals": clean(official.get("expected_goals")),
                "expected_assists": clean(official.get("expected_assists")),
            },
            "features": {
                feature: clean(_number(inputs.get(feature), None), 3)
                for feature in TABLE_FEATURES
            }
            if inputs is not None
            else {},
            "in_squad": player_id in roles,
            "role": roles.get(player_id, ""),
            "decision": decision.get("status"),
            "decision_reason": decision.get("reason"),
            "rank_in_position": decision.get("rank_in_position"),
        }
        if "cold_start_score" in record:
            row["cold_start_score"] = clean(record.get("cold_start_score"), 3)
        rows.append(row)
    rows.sort(key=lambda item: -(item["expected_points"] or 0.0))
    return rows


def model_agreement(predictions: pd.DataFrame) -> Dict[str, Any]:
    """Per-model distributions, correlations and the biggest disagreements."""
    inference = predictions.attrs.get("inference", {}) or {}
    payload = inference.get("model_predictions") or {}
    points = payload.get("points") or {}
    ids = payload.get("ids") or []
    if not points or not ids:
        return {"available": False}
    frame = pd.DataFrame(points, index=ids).apply(pd.to_numeric, errors="coerce")
    names = list(frame.columns)
    ensemble = (
        predictions.drop_duplicates("id")
        .set_index("id")["expected_points"]
        .reindex(frame.index)
        .astype(float)
    )
    distributions = {}
    for name in names:
        series = frame[name].dropna()
        distributions[name] = {
            "mean": clean(series.mean(), 3),
            "std": clean(series.std(), 3),
            "min": clean(series.min(), 3),
            "p90": clean(series.quantile(0.9), 3),
            "max": clean(series.max(), 3),
            "mean_abs_diff_from_ensemble": clean((series - ensemble).abs().mean(), 3),
            "top_pick_overlap": None,
        }
    correlation = {
        left: {right: _pearson(frame[left], frame[right]) for right in names}
        for left in names
    }
    top_ensemble = set(ensemble.sort_values(ascending=False).head(30).index)
    for name in names:
        top_model = set(frame[name].sort_values(ascending=False).head(30).index)
        distributions[name]["top_pick_overlap"] = (
            round(len(top_model & top_ensemble) / 30, 3) if top_ensemble else None
        )
    spread = frame.std(axis=1, ddof=0)
    labels = predictions.drop_duplicates("id").set_index("id")
    disagreements = []
    for player_id in spread.sort_values(ascending=False).head(15).index:
        row = labels.loc[player_id] if player_id in labels.index else {}
        disagreements.append(
            {
                "id": clean(player_id),
                "web_name": clean(row.get("web_name") if len(row) else None),
                "team_name": clean(row.get("team_name") if len(row) else None),
                "expected_points": clean(ensemble.get(player_id), 3),
                "spread": clean(spread.get(player_id), 3),
                "models": {name: clean(frame.loc[player_id, name], 3) for name in names},
            }
        )
    histogram_counts, edges = np.histogram(spread.dropna(), bins=12)
    return {
        "available": True,
        "models": names,
        "distributions": distributions,
        "correlation": correlation,
        "disagreements": disagreements,
        "spread_histogram": {
            "counts": [int(count) for count in histogram_counts],
            "edges": [round(float(edge), 3) for edge in edges],
        },
        "mean_spread": clean(spread.mean(), 4),
    }


# ----------------------------------------------------------------------
# Per-player explanations
# ----------------------------------------------------------------------


def _position_baselines(players: pd.DataFrame, position: int) -> Dict[str, float]:
    peers = players.loc[players["element_type"].map(position_code).eq(position)]
    if "fixture_count" in peers.columns:
        playing = peers.loc[pd.to_numeric(peers["fixture_count"], errors="coerce").gt(0)]
        if not playing.empty:
            peers = playing
    baselines = {}
    for feature in HISTORY_FEATURES:
        if feature not in peers.columns:
            continue
        values = pd.to_numeric(peers[feature], errors="coerce").dropna()
        if not values.empty:
            baselines[feature] = float(values.median())
    return baselines


def _percentile_in_position(
    players: pd.DataFrame, position: int, feature: str, value: Any
) -> Optional[float]:
    if feature not in players.columns or value is None or pd.isna(value):
        return None
    peers = pd.to_numeric(
        players.loc[players["element_type"].map(position_code).eq(position), feature],
        errors="coerce",
    ).dropna()
    if peers.empty:
        return None
    return round(float((peers <= float(value)).mean() * 100.0), 1)


def explain_player(
    scout: Any,
    inputs: Any,
    predictions: pd.DataFrame,
    player_id: int,
    *,
    trace: Optional[Mapping[str, Any]] = None,
    team: Optional[pd.DataFrame] = None,
    official: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Explain one player's projection with the live ensemble.

    Each input's contribution is how much the projection falls when that
    input alone is set to the median of players in his position. Inputs
    interact, so contributions need not add up to the gap to a typical
    player; the difference is reported as ``interactions``.
    """
    players = inputs.players.reset_index(drop=True)
    ids = pd.to_numeric(players["id"], errors="coerce")
    matches = np.flatnonzero(ids.to_numpy() == int(player_id))
    if not len(matches):
        raise KeyError(player_id)
    row_number = int(matches[0])
    player = players.iloc[row_number]
    position = position_code(player.get("element_type"))
    fixture_rows = inputs.fixture_rows
    own_rows = fixture_rows.index[fixture_rows["_player_row"].eq(row_number)].tolist()
    availability = _number(player.get("availability_factor"), 1.0)
    official = dict(official or {})

    prediction_row = predictions.loc[pd.to_numeric(predictions["id"], errors="coerce").eq(int(player_id))]
    served_points = (
        _number(prediction_row.iloc[0]["expected_points"]) if len(prediction_row) else None
    )
    decision = ((trace or {}).get("decisions") or {}).get(int(player_id), {})
    role = ""
    if team is not None and "id" in team.columns:
        member = team.loc[pd.to_numeric(team["id"], errors="coerce").eq(int(player_id))]
        if len(member):
            role = str(member.iloc[0].get("role") or "")

    base = {
        "id": int(player_id),
        "web_name": clean(player.get("web_name")),
        "team_name": clean(player.get("team_name")),
        "position": POSITION_SHORT.get(position, "?"),
        "gameweek": int(inputs.gameweek),
        "inputs_origin": inputs.origin,
        "approximate": bool(inputs.approximate),
        "fixtures": _fixtures_of(player.to_dict()),
        "availability": {
            "factor": clean(availability, 3),
            "status": clean(official.get("status", player.get("status"))),
            "news": clean(official.get("news")) or "",
            "chance_of_playing_next_round": clean(
                official.get("chance_of_playing_next_round",
                             player.get("chance_of_playing_next_round"))
            ),
        },
        "served_points": clean(served_points, 3),
        "decision": decision,
        "role": role,
    }
    if not own_rows:
        base.update(
            expected_points=0.0,
            reasons=[
                f"{player.get('web_name')} has no fixture in GW{inputs.gameweek}, "
                "so the projection is 0."
            ]
            + _selection_sentences(player, decision, role, team),
            contributions=[],
            groups=[],
            models={},
            fixture_sensitivity=[],
        )
        return base

    model_input = inputs.model_input
    originals = model_input.loc[own_rows]
    baselines = _position_baselines(players, position)
    ablated_features = [
        feature
        for feature in HISTORY_FEATURES
        if feature in baselines
        and feature in originals.columns
        and not np.allclose(
            pd.to_numeric(originals[feature], errors="coerce").to_numpy(dtype=float),
            baselines[feature],
            equal_nan=False,
        )
    ]

    blocks: List[pd.DataFrame] = [originals]
    labels: List[Tuple[str, Any]] = [("original", None)]
    for feature in ablated_features:
        block = originals.copy()
        block[feature] = baselines[feature]
        blocks.append(block)
        labels.append(("feature", feature))
    typical = originals.copy()
    for feature, value in baselines.items():
        if feature in typical.columns:
            typical[feature] = value
    blocks.append(typical)
    labels.append(("typical", None))

    # Every club that appears as a player's club or as anyone's opponent.
    clubs = set(players["team_name"].dropna().astype(str))
    if "opponent_team_name" in fixture_rows.columns:
        clubs |= set(fixture_rows["opponent_team_name"].dropna().astype(str))
    teams = sorted(club for club in clubs if club != str(player.get("team_name")))
    first = originals.iloc[[0]]
    sensitivity_rows = []
    for opponent in teams:
        for home in (True, False):
            candidate = first.copy()
            candidate["opponent_team_name"] = opponent
            candidate["was_home"] = home
            sensitivity_rows.append(candidate)
    sensitivity_start = sum(len(block) for block in blocks)
    batch = pd.concat(blocks + sensitivity_rows, ignore_index=True)

    run = scout._run_models(batch)
    ensemble, diagnostics = scout._ensemble_from_run(run, minimum=1)
    rows_per_block = len(originals)

    def block_sum(position_index: int, values: np.ndarray) -> float:
        start = position_index * rows_per_block
        return float(values[start : start + rows_per_block].sum())

    match_points = block_sum(0, ensemble)
    typical_points = block_sum(len(blocks) - 1, ensemble)
    contributions = []
    for index, (kind, feature) in enumerate(labels):
        if kind != "feature":
            continue
        delta = match_points - block_sum(index, ensemble)
        value = pd.to_numeric(originals[feature], errors="coerce").mean()
        contributions.append(
            {
                "feature": feature,
                "label": FEATURE_LABELS.get(feature, feature),
                "group": FEATURE_GROUP.get(feature, "Other"),
                "value": clean(value, 3),
                "baseline": clean(baselines[feature], 3),
                "percentile": _percentile_in_position(players, position, feature, value),
                "contribution": round(delta, 3),
            }
        )
    contributions.sort(key=lambda item: -abs(item["contribution"]))

    groups: Dict[str, float] = {}
    for item in contributions:
        groups[item["group"]] = groups.get(item["group"], 0.0) + item["contribution"]
    group_rows = [
        {"group": name, "contribution": round(value, 3)}
        for name, value in sorted(groups.items(), key=lambda item: -abs(item[1]))
    ]
    interactions = (match_points - typical_points) - sum(groups.values())

    models = {}
    for index, name in enumerate(run.names):
        raw = float(run.matrix[index][:rows_per_block].sum())
        models[name] = {
            "match_points": round(raw, 3),
            "expected_points": round(raw * availability, 3),
            "weight": round(float(run.weights[index]), 4),
        }

    sensitivity = []
    for offset, candidate in enumerate(sensitivity_rows):
        sensitivity.append(
            {
                "opponent": str(candidate["opponent_team_name"].iloc[0]),
                "home": bool(candidate["was_home"].iloc[0]),
                "points": round(float(ensemble[sensitivity_start + offset]), 3),
            }
        )
    sensitivity.sort(key=lambda item: -item["points"])
    actual_opponent = first["opponent_team_name"].iloc[0]
    actual_home = first["was_home"].iloc[0]
    fixture_rank = next(
        (
            rank
            for rank, item in enumerate(sensitivity, start=1)
            if item["opponent"] == str(actual_opponent)
            and bool(item["home"]) == bool(actual_home)
        ),
        None,
    )
    expected = match_points * availability
    waterfall = [
        {"step": f"Typical {POSITION_NAMES.get(position, 'player').lower()}", "value": round(typical_points, 3), "kind": "start"},
        *(
            {"step": row["group"], "value": row["contribution"], "kind": "delta"}
            for row in group_rows
        ),
        {"step": "Interactions", "value": round(interactions, 3), "kind": "delta"},
        {
            "step": "Availability",
            "value": round(expected - match_points, 3),
            "kind": "delta",
        },
        {"step": "Projection", "value": round(expected, 3), "kind": "end"},
    ]
    spread = (
        float(np.std([item["expected_points"] for item in models.values()]))
        if len(models) > 1
        else 0.0
    )
    base.update(
        expected_points=round(expected, 3),
        match_points=round(match_points, 3),
        typical_points=round(typical_points, 3),
        interactions=round(interactions, 3),
        contributions=contributions,
        groups=group_rows,
        waterfall=waterfall,
        models=models,
        model_spread=round(spread, 3),
        weights=diagnostics.get("weights", {}),
        fixture_sensitivity=sensitivity,
        fixture_rank=fixture_rank,
        fixture_options=len(sensitivity),
    )
    base["reasons"] = _explanation_sentences(
        player=player,
        position=position,
        gameweek=int(inputs.gameweek),
        expected=expected,
        match_points=match_points,
        typical_points=typical_points,
        contributions=contributions,
        availability=availability,
        news=base["availability"]["news"],
        spread=spread,
        models=models,
        fixture_rank=fixture_rank,
        fixture_options=len(sensitivity),
        fixtures=base["fixtures"],
    ) + _selection_sentences(player, decision, role, team)
    return base


def _fixture_label(fixtures: Sequence[Mapping[str, Any]]) -> str:
    if not fixtures:
        return "no fixture"
    parts = []
    for fixture in fixtures:
        venue = "H" if fixture.get("home") else "A"
        difficulty = fixture.get("difficulty")
        suffix = f", FDR {int(difficulty)}" if difficulty is not None else ""
        parts.append(f"{fixture.get('opponent')} ({venue}{suffix})")
    return " and ".join(parts)


def _explanation_sentences(
    *,
    player: pd.Series,
    position: int,
    gameweek: int,
    expected: float,
    match_points: float,
    typical_points: float,
    contributions: Sequence[Mapping[str, Any]],
    availability: float,
    news: str,
    spread: float,
    models: Mapping[str, Mapping[str, float]],
    fixture_rank: Optional[int],
    fixture_options: int,
    fixtures: Sequence[Mapping[str, Any]],
) -> List[str]:
    name = player.get("web_name")
    noun = POSITION_NAMES.get(position, "player").lower()
    sentences = [
        f"{name} is projected {expected:.2f} points in GW{gameweek} against "
        f"{_fixture_label(fixtures)}; a typical {noun} with the same fixture "
        f"would get {typical_points:.2f}."
    ]
    positive = [item for item in contributions if item["contribution"] > 0.05][:3]
    negative = [item for item in contributions if item["contribution"] < -0.05][:2]
    if positive:
        sentences.append(
            "Biggest boosts: "
            + "; ".join(
                f"{item['label']} {item['value']:.2f} vs {item['baseline']:.2f} typical "
                f"(+{item['contribution']:.2f})"
                for item in positive
                if item["value"] is not None
            )
            + "."
        )
    if negative:
        sentences.append(
            "Holding him back: "
            + "; ".join(
                f"{item['label']} {item['value']:.2f} vs {item['baseline']:.2f} typical "
                f"({item['contribution']:.2f})"
                for item in negative
                if item["value"] is not None
            )
            + "."
        )
    if len(fixtures) > 1:
        sentences.append(
            f"Double gameweek: both fixtures are projected and summed ({match_points:.2f} before availability)."
        )
    if fixture_rank is not None and fixture_options:
        sentences.append(
            f"The models rate this fixture #{fixture_rank} of {fixture_options} possible "
            "opponent and venue combinations for him (1 = best)."
        )
    if availability < 1.0:
        lost = match_points - expected
        detail = f" ({news})" if news else ""
        sentences.append(
            f"Availability {availability * 100:.0f}%{detail} removes {lost:.2f} points."
        )
    if len(models) > 1:
        lowest = min(models.items(), key=lambda item: item[1]["expected_points"])
        highest = max(models.items(), key=lambda item: item[1]["expected_points"])
        agreement = "agree closely" if spread < 0.35 else "disagree" if spread > 0.8 else "broadly agree"
        sentences.append(
            f"The models {agreement} (spread {spread:.2f}): {lowest[0]} "
            f"{lowest[1]['expected_points']:.2f} to {highest[0]} {highest[1]['expected_points']:.2f}."
        )
    return sentences


def _selection_sentences(
    player: pd.Series,
    decision: Mapping[str, Any],
    role: str,
    team: Optional[pd.DataFrame],
) -> List[str]:
    if not decision:
        return []
    status = decision.get("status")
    sentences = []
    if status in {"selected", "locked"}:
        sentence = f"Selected: {decision.get('reason')}"
        if decision.get("rank_in_position"):
            sentence += f" (ranked #{decision['rank_in_position']} in his position)"
        sentences.append(sentence + ".")
    elif decision.get("reason"):
        sentences.append(f"Not selected: {decision['reason']}.")
    if role and team is not None and len(team) >= 2:
        reasoning = captaincy_reasoning(team)
        if role == "captain" and reasoning:
            sentences.append(
                f"Captain: highest projection in the squad, {reasoning['captain_margin']:.2f} "
                f"ahead of {reasoning['vice']['web_name']}."
            )
        elif role == "vice" and reasoning:
            sentences.append(
                f"Vice-captain: second-highest projection, {reasoning['captain_margin']:.2f} "
                f"behind {reasoning['captain']['web_name']}."
            )
    elif status == "selected" and team is not None and len(team):
        captain = team.iloc[0]
        gap = _number(captain.get("expected_points")) - _number(
            team.loc[pd.to_numeric(team["id"], errors="coerce").eq(int(player["id"])), "expected_points"].sum()
        )
        sentences.append(f"{gap:.2f} points behind the captain, {captain.get('web_name')}.")
    return sentences


def cold_start_explanation(predictions: pd.DataFrame, player_id: int) -> Dict[str, Any]:
    """GW1 ranks by ownership, not models: explain that formula."""
    rows = predictions.loc[pd.to_numeric(predictions["id"], errors="coerce").eq(int(player_id))]
    if rows.empty:
        raise KeyError(player_id)
    row = rows.iloc[0]
    ownership = _number(row.get("selected_by_percent"))
    availability = _number(row.get("availability_factor"), 1.0)
    fixtures = int(_number(row.get("fixture_count")))
    return {
        "id": int(player_id),
        "web_name": clean(row.get("web_name")),
        "team_name": clean(row.get("team_name")),
        "strategy": "ownership-cold-start",
        "expected_points": clean(_number(row.get("expected_points")), 3),
        "reasons": [
            "GW1 has no match history yet, so the models are skipped.",
            f"Score = log(1 + ownership {ownership:.1f}%) x availability "
            f"{availability:.2f} x has-fixture ({'yes' if fixtures else 'no'}).",
        ],
        "contributions": [],
        "models": {},
    }


# ----------------------------------------------------------------------
# Forecast changes ("AI moves") between prediction runs
# ----------------------------------------------------------------------


def forecast_diff(
    previous: pd.DataFrame,
    current: pd.DataFrame,
    select: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    current_players: Optional[Mapping[int, Mapping[str, Any]]] = None,
    movers: int = 8,
) -> Dict[str, Any]:
    """What changed in the AI squad between two forecasts, and why."""
    old_team = select(previous)
    new_team = select(current)
    old_ids = [int(value) for value in old_team["id"]]
    new_ids = [int(value) for value in new_team["id"]]
    previous_rows = previous.drop_duplicates("id").set_index("id")
    current_rows = current.drop_duplicates("id").set_index("id")
    current_players = current_players or {}

    def reason(player_id: int, *, joined: bool) -> str:
        before = previous_rows.loc[player_id] if player_id in previous_rows.index else None
        after = current_rows.loc[player_id] if player_id in current_rows.index else None
        parts = []
        if before is not None and after is not None:
            old_factor = _number(before.get("availability_factor"), 1.0)
            new_factor = _number(after.get("availability_factor"), 1.0)
            if abs(old_factor - new_factor) > 1e-6:
                news = (current_players.get(player_id) or {}).get("news")
                parts.append(
                    f"availability {old_factor * 100:.0f}% -> {new_factor * 100:.0f}%"
                    + (f" ({news})" if news else "")
                )
            if str(before.get("opponent_team_name")) != str(after.get("opponent_team_name")):
                parts.append(
                    f"fixture {before.get('opponent_team_name')} -> {after.get('opponent_team_name')}"
                )
            delta = _number(after.get("expected_points")) - _number(before.get("expected_points"))
            parts.append(f"projection {delta:+.2f}")
        elif after is None:
            parts.append("no longer projected (not selectable)")
        else:
            parts.append("newly projected")
        if not parts or (len(parts) == 1 and parts[0].startswith("projection")):
            parts.append(
                "overtook a rival" if joined else "overtaken by a higher projection or club limit"
            )
        return "; ".join(parts)

    def describe(player_id: int, *, joined: bool) -> Dict[str, Any]:
        row = current_rows.loc[player_id] if player_id in current_rows.index else (
            previous_rows.loc[player_id] if player_id in previous_rows.index else {}
        )
        return {
            "id": player_id,
            "web_name": clean(row.get("web_name") if len(row) else None),
            "team_name": clean(row.get("team_name") if len(row) else None),
            "before": clean(_number(previous_rows["expected_points"].get(player_id), None), 2)
            if player_id in previous_rows.index
            else None,
            "after": clean(_number(current_rows["expected_points"].get(player_id), None), 2)
            if player_id in current_rows.index
            else None,
            "reason": reason(player_id, joined=joined),
        }

    joined = [describe(player_id, joined=True) for player_id in new_ids if player_id not in old_ids]
    left = [describe(player_id, joined=False) for player_id in old_ids if player_id not in new_ids]
    old_captain = int(old_team.iloc[0]["id"]) if len(old_team) else None
    new_captain = int(new_team.iloc[0]["id"]) if len(new_team) else None

    shared = previous_rows.index.intersection(current_rows.index)
    deltas = (
        pd.to_numeric(current_rows.loc[shared, "expected_points"], errors="coerce")
        - pd.to_numeric(previous_rows.loc[shared, "expected_points"], errors="coerce")
    ).dropna()
    biggest = deltas.abs().sort_values(ascending=False).head(movers).index
    moved = [
        {
            **describe(int(player_id), joined=True),
            "delta": clean(deltas.get(player_id), 3),
        }
        for player_id in biggest
        if abs(float(deltas.get(player_id))) >= 0.01
    ]
    captain_changed = old_captain != new_captain
    return {
        "squad_in": joined,
        "squad_out": left,
        "captain_changed": captain_changed,
        "captain_before": describe(old_captain, joined=False)
        if captain_changed and old_captain
        else None,
        "captain_after": describe(new_captain, joined=True)
        if captain_changed and new_captain
        else None,
        "top_movers": moved,
        "projected_total_before": round(float(old_team["expected_points"].sum()), 2),
        "projected_total_after": round(float(new_team["expected_points"].sum()), 2),
    }


def squad_diff(
    base_team: pd.DataFrame, other_team: pd.DataFrame
) -> Dict[str, Any]:
    """Players in and out between two squads, and the projection change."""
    base_ids = {int(value) for value in base_team["id"]}
    other_ids = {int(value) for value in other_team["id"]}
    def brief_rows(team: pd.DataFrame, ids: set) -> List[Dict[str, Any]]:
        rows = team.loc[pd.to_numeric(team["id"], errors="coerce").isin(ids)]
        return [_brief(row) for _, row in rows.iterrows()]
    return {
        "in": brief_rows(other_team, other_ids - base_ids),
        "out": brief_rows(base_team, base_ids - other_ids),
        "projected_total_live": round(float(base_team["expected_points"].sum()), 2),
        "projected_total_sandbox": round(float(other_team["expected_points"].sum()), 2),
        "captain_live": _brief(base_team.iloc[0]) if len(base_team) else None,
        "captain_sandbox": _brief(other_team.iloc[0]) if len(other_team) else None,
    }


# ----------------------------------------------------------------------
# Realised accuracy of archived forecasts
# ----------------------------------------------------------------------


def actual_points(live_payload: Mapping[str, Any]) -> Dict[int, Dict[str, float]]:
    """``{player_id: {"points", "minutes"}}`` from an official live payload."""
    result = {}
    for element in (live_payload or {}).get("elements", []) or []:
        try:
            player_id = int(element.get("id", element.get("element_id")))
        except (TypeError, ValueError):
            continue
        stats = element.get("stats") or {}
        result[player_id] = {
            "points": _number(stats.get("total_points")),
            "minutes": _number(stats.get("minutes")),
        }
    return result


def _metrics(predicted: pd.Series, actual: pd.Series) -> Dict[str, Any]:
    errors = predicted - actual
    return {
        "n": int(len(predicted)),
        "mae": clean(errors.abs().mean(), 4),
        "rmse": clean(math.sqrt(float((errors ** 2).mean())) if len(errors) else None, 4),
        "bias": clean(errors.mean(), 4),
        "pearson": _pearson(predicted, actual),
        "spearman": _spearman(predicted, actual),
    }


def gameweek_accuracy(
    forecast: pd.DataFrame,
    actuals: Mapping[int, Mapping[str, float]],
    *,
    select: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None,
    event: Optional[Mapping[str, Any]] = None,
    played_only: bool = False,
    top_n: int = 20,
) -> Dict[str, Any]:
    """Compare one gameweek's forecast with what happened."""
    frame = forecast.drop_duplicates("id").copy()
    frame["id"] = pd.to_numeric(frame["id"], errors="coerce")
    frame = frame.dropna(subset=["id"])
    frame["id"] = frame["id"].astype(int)
    frame["predicted"] = pd.to_numeric(frame["expected_points"], errors="coerce").fillna(0.0)
    frame["actual"] = frame["id"].map(lambda pid: (actuals.get(pid) or {}).get("points", 0.0))
    frame["minutes"] = frame["id"].map(lambda pid: (actuals.get(pid) or {}).get("minutes", 0.0))
    frame["_position"] = frame["element_type"].map(position_code)
    scored = frame.loc[frame["minutes"].gt(0)] if played_only else frame

    result: Dict[str, Any] = {
        "gameweek": int(forecast.attrs.get("gameweek") or 0),
        "captured_at": forecast.attrs.get("forecast_captured_at"),
        "freeze_method": forecast.attrs.get("freeze_method"),
        "strategy": (forecast.attrs.get("inference") or {}).get("strategy"),
        **_metrics(scored["predicted"], scored["actual"]),
    }
    top_predicted = set(frame.nlargest(top_n, "predicted")["id"])
    top_actual = set(frame.nlargest(top_n, "actual")["id"])
    result["top_precision"] = round(len(top_predicted & top_actual) / top_n, 3) if top_n else None
    result["by_position"] = {
        POSITION_SHORT[position]: {
            **_metrics(group["predicted"], group["actual"]),
            "predicted_mean": clean(group["predicted"].mean(), 3),
            "actual_mean": clean(group["actual"].mean(), 3),
        }
        for position, group in scored.groupby("_position")
        if position in POSITION_SHORT
    }

    model_points = model_points_by_id(forecast.attrs.get("inference", {}))
    if model_points:
        names = sorted({name for values in model_points.values() for name in values})
        per_model = {}
        for name in names:
            predicted = scored["id"].map(lambda pid: (model_points.get(pid) or {}).get(name))
            mask = predicted.notna()
            per_model[name] = _metrics(
                predicted[mask].astype(float), scored.loc[mask, "actual"].astype(float)
            )
        result["models"] = per_model

    if select is not None:
        try:
            team = select(forecast)
            records = team.to_dict(orient="records")
            lineup, total = best_lineup(records)
            captain = max(lineup, key=lambda player: _number(player.get("expected_points")))
            starting_actual = sum(
                (actuals.get(int(player["id"])) or {}).get("points", 0.0) for player in lineup
            )
            captain_actual = (actuals.get(int(captain["id"])) or {}).get("points", 0.0)
            squad_actuals = [
                ((actuals.get(int(player["id"])) or {}).get("points", 0.0), player)
                for player in records
            ]
            best_captain = max(squad_actuals, key=lambda item: item[0]) if squad_actuals else None
            result["squad"] = {
                "projected": round(total + _number(captain.get("expected_points")), 2),
                "actual": round(starting_actual + captain_actual, 2),
                "captain": clean(captain.get("web_name")),
                "captain_actual": clean(captain_actual, 1),
                "best_captain": clean(best_captain[1].get("web_name")) if best_captain else None,
                "best_captain_actual": clean(best_captain[0], 1) if best_captain else None,
                "average_entry_score": clean((event or {}).get("average_entry_score")),
                "highest_score": clean((event or {}).get("highest_score")),
            }
        except Exception as error:  # A squad problem must not hide metrics.
            result["squad"] = {"error": str(error)}
    result["_pairs"] = scored[["predicted", "actual"]].to_numpy().tolist()
    return result


def calibration(pairs: Sequence[Sequence[float]], buckets: int = 10) -> List[Dict[str, Any]]:
    """Mean actual points per predicted-points decile."""
    if not pairs:
        return []
    frame = pd.DataFrame(pairs, columns=["predicted", "actual"])
    try:
        frame["bucket"] = pd.qcut(
            frame["predicted"].rank(method="first"), buckets, labels=False
        )
    except ValueError:
        return []
    return [
        {
            "decile": int(bucket) + 1,
            "predicted": round(float(group["predicted"].mean()), 3),
            "actual": round(float(group["actual"].mean()), 3),
            "n": int(len(group)),
        }
        for bucket, group in frame.groupby("bucket")
    ]


def accuracy_summary(gameweeks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Season-level accuracy from per-gameweek results (pairs are consumed)."""
    pairs: List[Sequence[float]] = []
    for item in gameweeks:
        pairs.extend(item.pop("_pairs", []))
    summary: Dict[str, Any] = {"gameweeks": len(gameweeks)}
    if pairs:
        frame = pd.DataFrame(pairs, columns=["predicted", "actual"])
        summary.update(_metrics(frame["predicted"], frame["actual"]))
    squads = [item.get("squad") for item in gameweeks if item.get("squad") and "actual" in item["squad"]]
    if squads:
        summary["squad_actual_total"] = round(sum(squad["actual"] for squad in squads), 1)
        summary["squad_projected_total"] = round(sum(squad["projected"] for squad in squads), 1)
        averages = [squad["average_entry_score"] for squad in squads if squad.get("average_entry_score") is not None]
        if len(averages) == len(squads):
            summary["average_entry_total"] = float(sum(averages))
        summary["beat_average"] = sum(
            1
            for squad in squads
            if squad.get("average_entry_score") is not None
            and squad["actual"] > squad["average_entry_score"]
        )
    spearmans = [item["spearman"] for item in gameweeks if item.get("spearman") is not None]
    if spearmans:
        summary["mean_spearman"] = round(sum(spearmans) / len(spearmans), 4)
    summary["calibration"] = calibration(pairs)
    return summary

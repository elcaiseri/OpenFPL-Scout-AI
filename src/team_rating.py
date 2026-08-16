"""Score a published FPL manager squad against the Scout AI benchmark."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple


POSITION_CODES = {
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    "Goalkeeper": 1,
    "Defender": 2,
    "Midfielder": 3,
    "Forward": 4,
    "GK": 1,
    "DEF": 2,
    "MID": 3,
    "FWD": 4,
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _expected_points(player: Mapping[str, Any]) -> float:
    return max(0.0, _number(player.get("expected_points")))


def _position(player: Mapping[str, Any]) -> int:
    value = player.get("element_type", player.get("position"))
    if isinstance(value, str):
        value = value.strip()
    return POSITION_CODES.get(value, 0)


def _availability(player: Mapping[str, Any]) -> float:
    configured = player.get("availability_factor")
    if configured is not None:
        return min(1.0, max(0.0, _number(configured, 1.0)))
    if player.get("can_select") is False:
        return 0.0
    if str(player.get("status") or "a").casefold() in {"i", "s", "u", "n"}:
        return 0.0
    return 1.0


def _starting_players(players: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return [
        player
        for player in players
        if int(_number(player.get("pick_position"), 99)) <= 11
    ]


def _best_benchmark_lineup(
    players: Sequence[Mapping[str, Any]],
) -> Tuple[List[Mapping[str, Any]], float]:
    grouped: Dict[int, List[Mapping[str, Any]]] = {1: [], 2: [], 3: [], 4: []}
    for player in players:
        position = _position(player)
        if position in grouped:
            grouped[position].append(player)
    for group in grouped.values():
        group.sort(key=_expected_points, reverse=True)

    best_lineup: List[Mapping[str, Any]] = []
    best_total = -1.0
    for defenders in range(3, 6):
        for midfielders in range(2, 6):
            forwards = 10 - defenders - midfielders
            if forwards < 1 or forwards > 3:
                continue
            counts = {1: 1, 2: defenders, 3: midfielders, 4: forwards}
            if any(len(grouped[position]) < count for position, count in counts.items()):
                continue
            lineup = [
                player
                for position, count in counts.items()
                for player in grouped[position][:count]
            ]
            total = sum(_expected_points(player) for player in lineup)
            if total > best_total:
                best_lineup = lineup
                best_total = total

    if not best_lineup:
        raise ValueError("The AI benchmark could not produce a valid starting XI")
    return best_lineup, best_total


def _grade(rating: int) -> str:
    if rating >= 95:
        return "A+"
    if rating >= 90:
        return "A"
    if rating >= 85:
        return "A−"
    if rating >= 80:
        return "B+"
    if rating >= 75:
        return "B"
    if rating >= 70:
        return "B−"
    if rating >= 65:
        return "C+"
    if rating >= 60:
        return "C"
    if rating >= 50:
        return "D"
    return "E"


def rate_manager_team(
    manager_players: Sequence[Mapping[str, Any]],
    benchmark_players: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return a transparent 0–100 score for one published 15-player squad."""
    if len(manager_players) != 15:
        raise ValueError(
            f"A published FPL squad must contain 15 players; found {len(manager_players)}"
        )

    starters = _starting_players(manager_players)
    if len(starters) != 11:
        raise ValueError(
            f"A published FPL lineup must contain 11 starters; found {len(starters)}"
        )

    benchmark_lineup, benchmark_base = _best_benchmark_lineup(benchmark_players)
    manager_base = sum(_expected_points(player) for player in starters)

    manager_captain = next(
        (player for player in manager_players if player.get("is_captain")), None
    )
    benchmark_captain = max(benchmark_lineup, key=_expected_points)
    manager_captain_points = _expected_points(manager_captain or {})
    benchmark_captain_points = _expected_points(benchmark_captain)

    projection_ratio = min(1.0, manager_base / benchmark_base) if benchmark_base else 0.0
    captain_ratio = (
        min(1.0, manager_captain_points / benchmark_captain_points)
        if benchmark_captain_points
        else 0.0
    )
    availability_ratio = sum(_availability(player) for player in starters) / 11
    components = {
        "starting_xi": round(projection_ratio * 80, 1),
        "captaincy": round(captain_ratio * 10, 1),
        "availability": round(availability_ratio * 10, 1),
    }
    rating = round(sum(components.values()))

    manager_projection = sum(
        _expected_points(player) * max(0.0, _number(player.get("multiplier")))
        for player in manager_players
    )
    benchmark_projection = benchmark_base + benchmark_captain_points
    unavailable = [
        str(player.get("web_name") or "Unknown")
        for player in starters
        if _availability(player) < 1.0
    ]
    differentials = sum(
        1
        for player in manager_players
        if _number(player.get("selected_by_percent"), 100.0) < 10.0
    )

    strengths: List[str] = []
    risks: List[str] = []
    if projection_ratio >= 0.9:
        strengths.append("The starting XI projects close to the AI benchmark.")
    if captain_ratio >= 0.9:
        strengths.append("The captain is one of the strongest projected options.")
    if availability_ratio == 1.0:
        strengths.append("All 11 starters are currently marked available.")
    if differentials:
        strengths.append(
            f"The squad includes {differentials} differential pick"
            f"{'s' if differentials != 1 else ''} below 10% ownership."
        )

    projected_gap = max(0.0, benchmark_projection - manager_projection)
    if projected_gap >= 3.0:
        risks.append(
            f"The lineup trails the AI benchmark by {projected_gap:.1f} projected points."
        )
    if captain_ratio < 0.8:
        risks.append("The captain projects below the AI benchmark captain.")
    if unavailable:
        risks.append(
            "Availability needs checking: " + ", ".join(unavailable[:3]) + "."
        )
    if not strengths:
        strengths.append("The squad has a valid FPL structure and a complete starting XI.")
    if not risks:
        risks.append("No major model or availability risks were detected.")

    return {
        "rating": rating,
        "grade": _grade(rating),
        "projected_points": round(manager_projection, 2),
        "ai_projected_points": round(benchmark_projection, 2),
        "projected_gap": round(projected_gap, 2),
        "components": components,
        "captain": str((manager_captain or {}).get("web_name") or "Not set"),
        "differentials": differentials,
        "strengths": strengths,
        "risks": risks,
    }

"""Suggest affordable single transfers that raise a squad's projected points."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.team_rating import (
    _availability,
    _best_benchmark_lineup,
    _expected_points,
    _number,
    _position,
)

# Below this gain a transfer is noise in the projections, not advice.
MINIMUM_GAIN = 0.1
# Official statuses of players who cannot be bought to play: injured,
# suspended, unavailable (left the league) and not in the squad.
UNAVAILABLE_STATUSES = {"i", "s", "u", "n"}


def lineup_value(players: Sequence[Mapping[str, Any]]) -> float:
    """Projected points of the squad's best legal XI, with its best captain.

    Measured the way the team rating's AI benchmark is, so a transfer is
    worth what it adds to the team the manager can actually field.
    """
    lineup, total = _best_benchmark_lineup(players)
    return total + max(_expected_points(player) for player in lineup)


def currently_available(player: Mapping[str, Any]) -> bool:
    """Whether current official data lets a manager buy this player to play.

    A forecast's ``availability_factor`` can predate official news, so the
    current selectability, status and chance of playing decide first; the
    factor only matters for a player they all allow.
    """
    if player.get("can_select") is False:
        return False
    if str(player.get("status") or "a").casefold() in UNAVAILABLE_STATUSES:
        return False
    chance = _number(player.get("chance_of_playing_next_round"), None)
    if chance is not None and chance <= 0:
        return False
    return _availability(player) > 0


def clubs_over_limit(
    squad: Sequence[Mapping[str, Any]], max_per_club: int = 3
) -> Dict[str, int]:
    """Clubs with more than ``max_per_club`` players in the squad.

    A real-life transfer between Premier League clubs can leave a published
    squad above the limit; FPL then requires the next transfer out to come
    from that club.
    """
    clubs = Counter(player.get("team_name") for player in squad)
    return {
        str(club): count
        for club, count in sorted(clubs.items(), key=lambda item: str(item[0]))
        if club is not None and count > max_per_club
    }


def _within_club_limit(
    clubs: Counter, outgoing: Mapping[str, Any], incoming: Mapping[str, Any], limit: int
) -> bool:
    """Whether every club is within the limit once this swap is made."""
    after = clubs.copy()
    after[outgoing.get("team_name")] -= 1
    after[incoming.get("team_name")] += 1
    return all(count <= limit for count in after.values())


def _summary(player: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(player["id"]),
        "web_name": str(player.get("web_name") or "Unknown"),
        "team_name": player.get("team_name"),
        "position": _position(player),
        "price": _number(player.get("price"), None),
        "expected_points": round(_expected_points(player), 2),
    }


def suggest_transfers(
    squad: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    bank: Optional[float],
    *,
    limit: int = 3,
    max_per_club: int = 3,
) -> List[Dict[str, Any]]:
    """Return the best single transfers, largest projected gain first.

    Each suggestion swaps one squad player for a player in the same position
    whom current official data lets a manager buy to play
    (``currently_available``), whose current price is at most ``bank`` plus
    the outgoing player's current price, and after which every club has
    ``max_per_club`` players or fewer. So a squad already over the limit is
    only offered sales from that club, as FPL requires. FPL does not publish
    selling prices, which can be lower than current prices, so affordability
    is approximate. Each outgoing and incoming player appears in at most one
    suggestion. Without a bank, or without a price for a player, nothing is
    suggested for that player.
    """
    if bank is None or len(squad) != 15:
        return []
    squad_ids = {int(player["id"]) for player in squad}
    clubs = Counter(player.get("team_name") for player in squad)
    eligible = sorted(
        (
            player
            for player in candidates
            if int(player["id"]) not in squad_ids
            and currently_available(player)
            and _number(player.get("price"), None) is not None
        ),
        key=_expected_points,
        reverse=True,
    )
    current = lineup_value(squad)

    options = []
    for outgoing in squad:
        budget = _number(outgoing.get("price"), None)
        if budget is None:
            continue
        budget += bank
        position = _position(outgoing)
        # A player's own projection is all that moves the lineup, so the best
        # affordable options in the position are the best replacements. A few
        # are kept in case a better swap elsewhere takes the first.
        replacements = [
            player
            for player in eligible
            if _position(player) == position
            and _number(player.get("price")) <= budget + 1e-9
            and _within_club_limit(clubs, outgoing, player, max_per_club)
        ][:limit]
        for incoming in replacements:
            changed = [incoming if player is outgoing else player for player in squad]
            gain = lineup_value(changed) - current
            if gain >= MINIMUM_GAIN:
                options.append((gain, outgoing, incoming))

    suggestions: List[Dict[str, Any]] = []
    used_out, used_in = set(), set()
    # Equal gains (e.g. selling a starter or a benched player for the same
    # starter) favour selling the player projected lower.
    ranked = sorted(options, key=lambda item: (-item[0], _expected_points(item[1])))
    for gain, outgoing, incoming in ranked:
        if int(outgoing["id"]) in used_out or int(incoming["id"]) in used_in:
            continue
        used_out.add(int(outgoing["id"]))
        used_in.add(int(incoming["id"]))
        suggestions.append(
            {
                "out": _summary(outgoing),
                "in": _summary(incoming),
                "gain": round(gain, 2),
                "cost": round(
                    _number(incoming.get("price")) - _number(outgoing.get("price")), 1
                ),
            }
        )
        if len(suggestions) == limit:
            break
    return suggestions

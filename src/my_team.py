"""A manager's working squad: edits FPL cannot see yet, and the advice on it.

Official FPL publishes a manager's picks only after each deadline, so the
transfers, lineup, bench order, captaincy and chip a manager has already set
for the coming gameweek are invisible to the service. The owner dashboard
lets the manager enter them as a working squad (kept in their browser) and
add personal overrides: players to keep or avoid, and their own view of a
player's availability or projection. Everything here is pure: it validates
and scores what it is given and never changes a team.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.optimizer import SQUAD_QUOTAS, STARTER_MIN, selling_price
from src.team_rating import best_lineup

POSITION_SHORT = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
CHIP_NAMES = {
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
    "freehit": "Free Hit",
    "wildcard": "Wildcard",
}
FREE_TRANSFER_CHIPS = {"freehit", "wildcard"}
HIT_COST = 4
SQUAD_SIZE = 15
STARTERS = 11
MAX_PER_CLUB = 3
# Above this many uncertain players, autosub expectations are sampled.
EXACT_LIMIT = 10
SAMPLES = 2000
SEED = 7
# Net transfers this gameweek, as a share of estimated owners, beyond which
# a price move is flagged. FPL's algorithm is not public: a heuristic.
PRESSURE_THRESHOLD = 0.04
AUTOSUB_ASSUMPTION = (
    "Only flagged players (by their chance of playing) and players without a "
    "fixture are assumed to miss out; rotation is not modelled."
)


def _number(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _position(player: Mapping[str, Any]) -> int:
    value = player.get("element_type", player.get("position"))
    if isinstance(value, str):
        value = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4, "Goalkeeper": 1, "Defender": 2,
                 "Midfielder": 3, "Forward": 4}.get(value.strip(), value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _name(player: Optional[Mapping[str, Any]], player_id: Any = None) -> str:
    if player and player.get("web_name"):
        return str(player["web_name"])
    return f"#{player_id}" if player_id is not None else "Unknown"


def _slot(pick: Mapping[str, Any]) -> int:
    return int(_number(pick.get("position"), 99))


# ----------------------------------------------------------------------
# Picks: order, multipliers, validation
# ----------------------------------------------------------------------


def starters_and_bench(picks: Sequence[Mapping[str, Any]]) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    ordered = sorted(picks, key=_slot)
    return [pick for pick in ordered if _slot(pick) <= STARTERS], [
        pick for pick in ordered if _slot(pick) > STARTERS
    ]


def normalize_picks(
    picks: Sequence[Mapping[str, Any]], players: Mapping[int, Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """FPL's slot order: starters GK→FWD in 1–11, bench GK in 12, subs in order.

    The manager's relative order inside each group is kept, so the bench
    order of outfield substitutes (slots 13–15) is preserved.
    """
    starters, bench = starters_and_bench(picks)
    starters = sorted(starters, key=lambda pick: _position(players.get(int(pick["id"]), {})) or 9)
    keepers = [pick for pick in bench if _position(players.get(int(pick["id"]), {})) == 1]
    others = [pick for pick in bench if _position(players.get(int(pick["id"]), {})) != 1]
    return [
        {
            "id": int(pick["id"]),
            "position": slot,
            "is_captain": bool(pick.get("is_captain")),
            "is_vice_captain": bool(pick.get("is_vice_captain")),
        }
        for slot, pick in enumerate(starters + keepers + others, start=1)
    ]


def multipliers(picks: Sequence[Mapping[str, Any]], chip: Optional[str] = None) -> Dict[int, int]:
    """Points multiplier per player: the captain doubles (trebles with TC)."""
    result = {}
    for pick in picks:
        starting = _slot(pick) <= STARTERS or chip == "bboost"
        multiplier = 1 if starting else 0
        if pick.get("is_captain") and multiplier:
            multiplier = 3 if chip == "3xc" else 2
        result[int(pick["id"])] = multiplier
    return result


def formation(picks: Sequence[Mapping[str, Any]], players: Mapping[int, Mapping[str, Any]]) -> Optional[str]:
    starters, _ = starters_and_bench(picks)
    counts = Counter(_position(players.get(int(pick["id"]), {})) for pick in starters)
    if len(starters) != STARTERS:
        return None
    return f"{counts[2]}-{counts[3]}-{counts[4]}"


def validate_squad(
    picks: Sequence[Mapping[str, Any]],
    players: Mapping[int, Mapping[str, Any]],
    *,
    published_ids: Iterable[int] = (),
    bank_after: Optional[float] = None,
    chip: Optional[str] = None,
    chip_status: Optional[Mapping[str, Mapping[str, Any]]] = None,
    max_per_club: int = MAX_PER_CLUB,
) -> Dict[str, Any]:
    """Check a working squad against FPL's rules.

    Errors make the squad unusable; warnings are worth knowing. A club over
    the limit that the published squad already had (after a real-life
    transfer) is only a warning, as FPL allows it until the next transfer.
    """
    errors: List[str] = []
    warnings: List[str] = []
    ids = [int(pick["id"]) for pick in picks]
    published = [int(value) for value in published_ids]
    if len(ids) != SQUAD_SIZE:
        errors.append(f"A squad has 15 players; this one has {len(ids)}.")
    for player_id, count in Counter(ids).items():
        if count > 1:
            errors.append(f"{_name(players.get(player_id), player_id)} is picked {count} times.")
    unknown = [player_id for player_id in ids if player_id not in players]
    if unknown:
        errors.append(f"Unknown players: {', '.join(f'#{value}' for value in unknown)}.")
    slots = sorted(_slot(pick) for pick in picks)
    if slots != list(range(1, len(picks) + 1)):
        errors.append("Squad slots must run from 1 to 15 with no gaps or repeats.")

    known = {player_id: players[player_id] for player_id in set(ids) if player_id in players}
    counts = Counter(_position(player) for player in known.values())
    if len(known) == SQUAD_SIZE and any(counts[position] != quota for position, quota in SQUAD_QUOTAS.items()):
        errors.append(
            "A squad needs 2 GK, 5 DEF, 5 MID and 3 FWD; this one has "
            + ", ".join(f"{counts[position]} {POSITION_SHORT[position]}" for position in SQUAD_QUOTAS)
            + "."
        )

    clubs = Counter(str(player.get("team_name")) for player in known.values())
    before = Counter(str(players[pid].get("team_name")) for pid in published if pid in players)
    for club, count in sorted(clubs.items()):
        if count <= max_per_club or club == "None":
            continue
        if count <= before.get(club, 0):
            warnings.append(
                f"{count} players from {club} (over the limit of {max_per_club} after a real-life "
                "transfer): FPL lets you keep them, but your next transfer out must include one."
            )
        else:
            errors.append(f"{count} players from {club}: the limit is {max_per_club} per club.")

    starters, bench = starters_and_bench(picks)
    starter_counts = Counter(_position(players.get(int(pick["id"]), {})) for pick in starters)
    if len(starters) != STARTERS:
        errors.append(f"The starting XI needs 11 players; it has {len(starters)}.")
    elif len(known) == SQUAD_SIZE:
        if starter_counts[1] != 1:
            errors.append(f"The XI needs exactly one goalkeeper; it has {starter_counts[1]}.")
        for position in (2, 3, 4):
            if starter_counts[position] < STARTER_MIN[position]:
                errors.append(
                    f"The XI needs at least {STARTER_MIN[position]} {POSITION_SHORT[position]}; "
                    f"it has {starter_counts[position]}."
                )

    captains = [pick for pick in picks if pick.get("is_captain")]
    vices = [pick for pick in picks if pick.get("is_vice_captain")]
    starting_ids = {int(pick["id"]) for pick in starters}
    if len(captains) != 1:
        errors.append(f"Pick exactly one captain ({len(captains)} picked).")
    if len(vices) != 1:
        errors.append(f"Pick exactly one vice-captain ({len(vices)} picked).")
    if len(captains) == 1 and len(vices) == 1 and int(captains[0]["id"]) == int(vices[0]["id"]):
        errors.append("The captain and vice-captain must be different players.")
    for label, chosen in (("captain", captains), ("vice-captain", vices)):
        for pick in chosen:
            if int(pick["id"]) not in starting_ids:
                errors.append(
                    f"The {label}, {_name(players.get(int(pick['id'])), pick['id'])}, must be in the starting XI."
                )

    if bank_after is not None and bank_after < -1e-9:
        errors.append(f"Over budget by £{abs(bank_after):.1f}m.")

    for player_id in ids:
        if player_id in published or player_id not in players:
            continue
        player = players[player_id]
        if player.get("can_select") is False:
            errors.append(f"{_name(player)} can't be bought: FPL marks him unavailable.")
            continue
        status = str(player.get("status") or "a").casefold()
        chance = _number(player.get("chance_of_playing_next_round"), None)
        news = str(player.get("news") or "").strip()
        if status in {"i", "s", "u", "n"} or (chance is not None and chance <= 0):
            warnings.append(f"You're buying {_name(player)}, who is ruled out{f': {news}' if news else ''}.")
        elif chance is not None and chance < 100:
            warnings.append(f"You're buying {_name(player)}, {int(chance)}% to play{f': {news}' if news else ''}.")

    if chip and chip_status:
        status = chip_status.get(chip) or {}
        if status and not status.get("available_this_half", True):
            warnings.append(
                f"{CHIP_NAMES.get(chip, chip)} looks already played this half "
                f"(GW{', GW'.join(str(value) for value in status.get('played', []))}); estimate from public history."
            )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "formation": formation(picks, players) if not errors else None,
    }


# ----------------------------------------------------------------------
# What changed against the published squad
# ----------------------------------------------------------------------


def squad_changes(
    published_picks: Sequence[Mapping[str, Any]],
    draft_picks: Sequence[Mapping[str, Any]],
    *,
    players: Mapping[int, Mapping[str, Any]],
    selling: Mapping[int, Optional[float]],
    bank: float,
    free_transfers: Optional[int],
    chip: Optional[str] = None,
    bank_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Transfers, money, hits and lineup changes from published to working.

    Outgoing players are sold at their (estimated) selling price; incoming
    ones are bought at today's price. ``free_transfers`` of None means
    unlimited (no deadline has passed yet).
    """
    published_ids = [int(pick["id"]) for pick in sorted(published_picks, key=_slot)]
    draft_ids = [int(pick["id"]) for pick in sorted(draft_picks, key=_slot)]
    published_set, draft_set = set(published_ids), set(draft_ids)
    out_ids = [pid for pid in published_ids if pid not in draft_set]
    in_ids = [pid for pid in draft_ids if pid not in published_set]

    def price(player_id: int) -> float:
        return float(_number(players.get(player_id, {}).get("price"), 0.0))

    sold = sum(float(_number(selling.get(pid), price(pid))) for pid in out_ids)
    bought = sum(price(pid) for pid in in_ids)
    computed = round(float(bank) + sold - bought, 1)
    bank_after = round(float(bank_override), 1) if bank_override is not None else computed
    count = len(in_ids)
    chip_free = chip in FREE_TRANSFER_CHIPS
    unlimited = free_transfers is None
    hits = 0 if (chip_free or unlimited) else max(0, count - int(free_transfers))
    remaining = None if unlimited else (
        int(free_transfers) if chip_free else max(0, int(free_transfers) - count)
    )

    transfers = []
    for position in (1, 2, 3, 4):
        outs = [pid for pid in out_ids if _position(players.get(pid, {})) == position]
        ins = [pid for pid in in_ids if _position(players.get(pid, {})) == position]
        for out_id, in_id in itertools.zip_longest(outs, ins):
            transfers.append(
                {
                    "out": None if out_id is None else {
                        "id": out_id,
                        "web_name": _name(players.get(out_id), out_id),
                        "team_name": players.get(out_id, {}).get("team_name"),
                        "position": POSITION_SHORT.get(position),
                        "sell": round(float(_number(selling.get(out_id), price(out_id))), 1),
                    },
                    "in": None if in_id is None else {
                        "id": in_id,
                        "web_name": _name(players.get(in_id), in_id),
                        "team_name": players.get(in_id, {}).get("team_name"),
                        "position": POSITION_SHORT.get(position),
                        "buy": round(price(in_id), 1),
                    },
                }
            )

    published_starters = {int(p["id"]) for p in published_picks if _slot(p) <= STARTERS}
    draft_starters = {int(p["id"]) for p in draft_picks if _slot(p) <= STARTERS}
    kept = published_set & draft_set
    benched = sorted(pid for pid in kept if pid in published_starters and pid not in draft_starters)
    promoted = sorted(pid for pid in kept if pid not in published_starters and pid in draft_starters)

    def bench_order(picks: Sequence[Mapping[str, Any]]) -> List[int]:
        return [int(p["id"]) for p in sorted(picks, key=_slot) if _slot(p) > STARTERS and int(p["id"]) in kept]

    def role(picks: Sequence[Mapping[str, Any]], key: str) -> Optional[int]:
        return next((int(p["id"]) for p in picks if p.get(key)), None)

    old_captain, new_captain = role(published_picks, "is_captain"), role(draft_picks, "is_captain")
    old_vice, new_vice = role(published_picks, "is_vice_captain"), role(draft_picks, "is_vice_captain")
    bench_reordered = not benched and not promoted and bench_order(published_picks) != bench_order(draft_picks)

    summary: List[str] = []
    if count:
        detail = "free with the chip" if chip_free else (
            "unlimited before the first deadline" if unlimited else
            f"{min(count, int(free_transfers))} free" + (f", −{hits * HIT_COST}" if hits else "")
        )
        summary.append(f"{count} transfer{'s' if count != 1 else ''} ({detail})")
    if benched or promoted:
        summary.append(f"lineup: {len(promoted)} in, {len(benched)} benched")
    elif bench_reordered:
        summary.append("bench reordered")
    if old_captain != new_captain:
        summary.append(f"captain {_name(players.get(new_captain), new_captain)}")
    if old_vice != new_vice:
        summary.append(f"vice {_name(players.get(new_vice), new_vice)}")
    if chip:
        summary.append(CHIP_NAMES.get(chip, chip))
    if bank_override is not None and abs(bank_after - computed) > 1e-9:
        summary.append(f"bank set to £{bank_after:.1f}m")

    return {
        "transfers": transfers,
        "out_ids": out_ids,
        "in_ids": in_ids,
        "count": count,
        "free_transfers": free_transfers,
        "free_transfers_remaining": remaining,
        "hits": hits,
        "hit_cost": hits * HIT_COST,
        "sold": round(sold, 1),
        "bought": round(bought, 1),
        "bank": round(float(bank), 1),
        "bank_computed": computed,
        "bank_after": bank_after,
        "benched": benched,
        "promoted": promoted,
        "bench_reordered": bench_reordered,
        "captain": {"before": old_captain, "after": new_captain},
        "vice": {"before": old_vice, "after": new_vice},
        "chip": chip,
        "summary": summary,
        "edited": bool(summary),
    }


# ----------------------------------------------------------------------
# Personal overrides of availability and projection
# ----------------------------------------------------------------------


def override_value(
    expected: float,
    factor: Optional[float],
    override: Mapping[str, Any],
    raw: Optional[float] = None,
) -> Tuple[float, float, Optional[str]]:
    """A player's projection and availability under the manager's own view.

    ``expected`` already includes the availability ``factor``; the points
    if he plays are ``expected / factor``. For a player ruled out (factor 0)
    those points come from ``raw`` (a re-projection as if fit). Returns the
    new projection, the new factor and a note when it could not be done.
    """
    factor = 1.0 if factor is None else min(1.0, max(0.0, float(factor)))
    expected = max(0.0, float(expected))
    note = None
    availability = override.get("availability")
    if availability is not None:
        target = min(1.0, max(0.0, float(availability) / 100.0))
        if factor > 0:
            if_plays = expected / factor
        elif raw is not None:
            if_plays = max(0.0, float(raw))
        else:
            if_plays = 0.0
            note = "could not be re-projected as fit, so stays at 0"
        expected, factor = if_plays * target, target
    percent = override.get("xp_pct")
    if percent is not None:
        expected *= max(0.0, float(percent)) / 100.0
    return expected, factor, note


def needs_raw(factor: Optional[float], override: Mapping[str, Any]) -> bool:
    """Whether a re-projection as fit is needed for this override."""
    return (
        override.get("availability") is not None
        and float(override.get("availability") or 0) > 0
        and factor is not None
        and float(factor) <= 0
    )


def apply_overrides(
    records: Sequence[Mapping[str, Any]],
    overrides: Mapping[int, Mapping[str, Any]],
    raw: Optional[Mapping[int, float]] = None,
    model_points: Optional[Mapping[int, Mapping[str, float]]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, float]], List[str]]:
    """Projection records and per-model points with the overrides applied.

    Each changed record keeps ``base_expected_points`` and
    ``base_availability_factor``, and model points are scaled by the same
    ratio (or by the re-projection for a player ruled out).
    """
    raw = raw or {}
    model_points = {int(key): dict(value) for key, value in (model_points or {}).items()}
    notes: List[str] = []
    result = []
    for record in records:
        record = dict(record)
        player_id = int(record.get("id") or 0)
        override = overrides.get(player_id)
        if override and (override.get("availability") is not None or override.get("xp_pct") is not None):
            base = float(_number(record.get("expected_points"), 0.0))
            factor = _number(record.get("availability_factor"), None)
            expected, new_factor, note = override_value(base, factor, override, raw.get(player_id))
            record["base_expected_points"] = round(base, 3)
            record["base_availability_factor"] = factor
            record["expected_points"] = expected
            record["availability_factor"] = new_factor
            record["overridden"] = True
            if note:
                notes.append(f"{_name(record, player_id)} {note}.")
            models = model_points.get(player_id)
            if models:
                if base > 0:
                    ratio = expected / base
                    model_points[player_id] = {name: value * ratio for name, value in models.items()}
                else:
                    model_points[player_id] = {name: expected for name in models}
        result.append(record)
    return result, model_points, notes


def apply_overrides_frame(
    points: pd.DataFrame,
    factors: Optional[pd.DataFrame],
    overrides: Mapping[int, Mapping[str, Any]],
    raw_lookup: Optional[Callable[[int, Sequence[int]], Mapping[int, float]]] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """Horizon points (players × gameweeks) with the overrides applied.

    The manager's availability applies to every gameweek of the horizon.
    ``raw_lookup(gameweek, ids)`` re-projects players ruled out as if fit.
    """
    points = points.copy()
    notes: List[str] = []
    if factors is None:
        factors = pd.DataFrame(1.0, index=points.index, columns=points.columns)
    targets = [pid for pid in overrides if pid in points.index]
    for gameweek in points.columns:
        need = [
            pid for pid in targets
            if needs_raw(float(factors.at[pid, gameweek]) if pid in factors.index else 1.0, overrides[pid])
        ]
        raw: Mapping[int, float] = {}
        if need and raw_lookup is not None:
            try:
                raw = raw_lookup(int(gameweek), need) or {}
            except Exception as error:  # Re-projection is best effort.
                notes.append(f"GW{gameweek}: could not re-project {len(need)} player(s) as fit ({error}).")
        for pid in targets:
            factor = float(factors.at[pid, gameweek]) if pid in factors.index else 1.0
            value, _, note = override_value(float(points.at[pid, gameweek]), factor, overrides[pid], raw.get(pid))
            points.at[pid, gameweek] = value
            if note and pid in need:
                notes.append(f"GW{gameweek}: player #{pid} {note}.")
    return points, sorted(set(notes))


# ----------------------------------------------------------------------
# Expected points with automatic substitutions
# ----------------------------------------------------------------------


def _chance(player: Mapping[str, Any]) -> float:
    if _number(player.get("fixture_count"), 1.0) == 0:
        return 0.0
    factor = _number(player.get("availability_factor"), None)
    if factor is None:
        return 1.0
    return min(1.0, max(0.0, float(factor)))


def _if_plays(player: Mapping[str, Any]) -> float:
    chance = _chance(player)
    expected = max(0.0, float(_number(player.get("expected_points"), 0.0)))
    return expected / chance if chance > 0 else 0.0


def _formation_ok(counts: Mapping[int, int]) -> bool:
    return counts.get(1, 0) == 1 and all(
        counts.get(position, 0) >= STARTER_MIN[position] for position in (2, 3, 4)
    )


def _scenario_points(
    starters: Sequence[int],
    bench: Sequence[int],
    plays: Mapping[int, bool],
    positions: Mapping[int, int],
    values: Mapping[int, float],
    captain: Optional[int],
    vice: Optional[int],
    captain_extra: int,
    bench_boost: bool,
) -> Tuple[float, List[int], Optional[int]]:
    """Points of one scenario, the bench players used and who wore the armband."""
    if bench_boost:
        lineup = [pid for pid in list(starters) + list(bench) if plays[pid]]
        used: List[int] = []
    else:
        slots = list(starters)
        used = []
        for sub in bench:
            if not plays[sub]:
                continue
            for index, starter in enumerate(slots):
                if starter in used or plays[starter]:
                    continue
                if positions[sub] == 1 or positions[starter] == 1:
                    if positions[sub] != positions[starter]:
                        continue
                else:
                    counts = Counter(positions[pid] for pid in slots)
                    counts[positions[starter]] -= 1
                    counts[positions[sub]] += 1
                    if not _formation_ok(counts):
                        continue
                slots[index] = sub
                used.append(sub)
                break
        lineup = [pid for pid in slots if plays[pid]]
    total = sum(values[pid] for pid in lineup)
    armband = None
    if captain is not None and captain in lineup:
        armband = captain
    elif vice is not None and vice in lineup:
        armband = vice
    if armband is not None:
        total += captain_extra * values[armband]
    return total, used, armband


def expected_with_autosubs(
    players: Mapping[int, Mapping[str, Any]],
    picks: Sequence[Mapping[str, Any]],
    chip: Optional[str] = None,
    *,
    exact_limit: int = EXACT_LIMIT,
    samples: int = SAMPLES,
    seed: int = SEED,
) -> Dict[str, Any]:
    """Expected points of a 15-player pick with FPL's automatic substitutions.

    A player plays with his availability factor (0 without a fixture) and
    then scores his points-if-he-plays (xP / chance). Bench players come on
    in bench order, a goalkeeper only for the goalkeeper and an outfield
    player only if the formation stays legal; the vice takes the armband
    if the captain doesn't play. Exact over every combination of uncertain
    players up to ``exact_limit`` of them, then sampled with a fixed seed.
    """
    starters_picks, bench_picks = starters_and_bench(picks)
    starters = [int(pick["id"]) for pick in starters_picks]
    bench = [int(pick["id"]) for pick in bench_picks]
    ids = starters + bench
    positions = {pid: _position(players.get(pid, {})) for pid in ids}
    chances = {pid: _chance(players.get(pid, {})) for pid in ids}
    values = {pid: _if_plays(players.get(pid, {})) for pid in ids}
    captain = next((int(p["id"]) for p in picks if p.get("is_captain")), None)
    vice = next((int(p["id"]) for p in picks if p.get("is_vice_captain")), None)
    extra = 2 if chip == "3xc" else 1
    bench_boost = chip == "bboost"
    uncertain = [pid for pid in ids if 0.0 < chances[pid] < 1.0]
    fixed = {pid: chances[pid] >= 1.0 for pid in ids if pid not in uncertain}

    expected = 0.0
    bench_use = Counter()
    armband = Counter()
    if len(uncertain) <= exact_limit:
        method, count = "exact", 2 ** len(uncertain)
        for outcome in itertools.product((True, False), repeat=len(uncertain)):
            weight = 1.0
            plays = dict(fixed)
            for pid, played in zip(uncertain, outcome):
                plays[pid] = played
                weight *= chances[pid] if played else 1.0 - chances[pid]
            if weight == 0.0:
                continue
            total, used, wearer = _scenario_points(
                starters, bench, plays, positions, values, captain, vice, extra, bench_boost
            )
            expected += weight * total
            for pid in used:
                bench_use[pid] += weight
            if wearer is not None:
                armband[wearer] += weight
    else:
        method, count = "sampled", int(samples)
        rng = np.random.default_rng(seed)
        draws = rng.random((count, len(uncertain)))
        weight = 1.0 / count
        for row in draws:
            plays = dict(fixed)
            for pid, value in zip(uncertain, row):
                plays[pid] = bool(value < chances[pid])
            total, used, wearer = _scenario_points(
                starters, bench, plays, positions, values, captain, vice, extra, bench_boost
            )
            expected += weight * total
            for pid in used:
                bench_use[pid] += weight
            if wearer is not None:
                armband[wearer] += weight
    simple = sum(
        float(_number(players.get(pid, {}).get("expected_points"), 0.0))
        * (1 if (pid in starters or bench_boost) else 0)
        for pid in ids
    )
    if captain is not None:
        simple += extra * float(_number(players.get(captain, {}).get("expected_points"), 0.0))
    return {
        "expected_points": round(expected, 2),
        "without_autosubs": round(simple, 2),
        "autosub_value": round(expected - simple, 2),
        "method": method,
        "scenarios": count,
        "uncertain": uncertain,
        "bench_use": {pid: round(bench_use.get(pid, 0.0), 3) for pid in bench},
        "captain_plays": round(armband.get(captain, 0.0), 3) if captain is not None else None,
        "vice_armband": round(armband.get(vice, 0.0), 3) if vice is not None else None,
        "assumption": AUTOSUB_ASSUMPTION,
    }


# ----------------------------------------------------------------------
# Lineup, bench order and captaincy advice
# ----------------------------------------------------------------------


def _armband_value(players: Mapping[int, Mapping[str, Any]], captain: int, vice: int) -> float:
    chance = _chance(players.get(captain, {}))
    return chance * _if_plays(players.get(captain, {})) + (1 - chance) * _chance(
        players.get(vice, {})
    ) * _if_plays(players.get(vice, {}))


def best_armband(players: Mapping[int, Mapping[str, Any]], starters: Sequence[int]) -> Tuple[int, int]:
    """The captain and vice pair with the most expected armband points."""
    best_key: Optional[Tuple[float, ...]] = None
    best = (0, 0)
    for captain in starters:
        for vice in starters:
            if vice == captain:
                continue
            key = (
                round(_armband_value(players, captain, vice), 9),
                float(_number(players.get(captain, {}).get("expected_points"), 0.0)),
                float(_number(players.get(vice, {}).get("expected_points"), 0.0)),
                -captain,
                -vice,
            )
            if best_key is None or key > best_key:
                best_key, best = key, (captain, vice)
    return best


def _picks_from(starters: Sequence[int], bench: Sequence[int], captain: int, vice: int,
                players: Mapping[int, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    raw = [
        {"id": pid, "position": slot, "is_captain": pid == captain, "is_vice_captain": pid == vice}
        for slot, pid in enumerate(list(starters) + list(bench), start=1)
    ]
    return normalize_picks(raw, players)


def best_bench_order(
    players: Mapping[int, Mapping[str, Any]],
    starters: Sequence[int],
    bench: Sequence[int],
    captain: int,
    vice: int,
    chip: Optional[str] = None,
) -> Tuple[List[int], Dict[str, Any]]:
    """The bench order (keeper first) with the most expected points."""
    keepers = [pid for pid in bench if _position(players.get(pid, {})) == 1]
    outfield = [pid for pid in bench if _position(players.get(pid, {})) != 1]
    best_order: Optional[List[int]] = None
    best_result: Optional[Dict[str, Any]] = None
    # The current order is tried first, so ties keep it.
    for order in itertools.permutations(outfield):
        candidate = keepers + list(order)
        result = expected_with_autosubs(players, _picks_from(starters, candidate, captain, vice, players), chip)
        if best_result is None or result["expected_points"] > best_result["expected_points"] + 1e-9:
            best_order, best_result = candidate, result
    assert best_order is not None and best_result is not None
    return best_order, best_result


def lineup_advice(
    players: Mapping[int, Mapping[str, Any]],
    picks: Sequence[Mapping[str, Any]],
    chip: Optional[str] = None,
) -> Dict[str, Any]:
    """The best XI, bench order and armband for a 15-player squad.

    Two XIs are compared: the current one and the highest-projected legal
    one; each gets its best bench order and captain pair, and the better
    expectation with autosubs wins, so the advice is never worse than
    keeping the current lineup.
    """
    current_starters, current_bench = starters_and_bench(picks)
    starting = [int(pick["id"]) for pick in current_starters]
    benched = [int(pick["id"]) for pick in current_bench]
    squad_ids = starting + benched
    current = expected_with_autosubs(players, picks, chip)

    records = [{"id": pid, **players.get(pid, {})} for pid in squad_ids]
    lineup, _ = best_lineup(records)
    projected_xi = [int(player["id"]) for player in lineup]
    options = []
    for xi in (starting, projected_xi):
        rest = [pid for pid in squad_ids if pid not in xi]
        rest.sort(key=lambda pid: (benched.index(pid) if pid in benched else 99))
        captain, vice = best_armband(players, xi)
        order, result = best_bench_order(players, xi, rest, captain, vice, chip)
        options.append((result["expected_points"], xi, order, captain, vice, result))
    best = max(options, key=lambda item: item[0] + (1e-9 if item[1] is starting else 0.0))
    _, xi, order, captain, vice, result = best
    advised = _picks_from(xi, order, captain, vice, players)

    def xp(pid: int) -> float:
        return float(_number(players.get(pid, {}).get("expected_points"), 0.0))

    def label(pid: int) -> str:
        return _name(players.get(pid), pid)

    reasons: List[str] = []
    promoted = [pid for pid in xi if pid not in starting]
    dropped = [pid for pid in starting if pid not in xi]
    for incoming, outgoing in zip(sorted(promoted, key=xp, reverse=True), sorted(dropped, key=xp)):
        reasons.append(f"Start {label(incoming)} ({xp(incoming):.1f} xP) over {label(outgoing)} ({xp(outgoing):.1f} xP).")
    current_captain = next((int(p["id"]) for p in picks if p.get("is_captain")), None)
    current_vice = next((int(p["id"]) for p in picks if p.get("is_vice_captain")), None)
    chance = _chance(players.get(captain, {}))
    captain_line = (
        f"Captain {label(captain)}: {_if_plays(players.get(captain, {})):.1f} points if he plays"
        + (f" ({chance:.0%} to play)" if chance < 1 else "")
        + "."
    )
    if current_captain != captain:
        captain_line = captain_line[:-1] + f", instead of {label(current_captain) if current_captain else 'nobody'}."
    reasons.append(captain_line)
    if chance < 1:
        reasons.append(f"Vice {label(vice)} takes the armband in the {1 - chance:.0%} of cases {label(captain)} misses out.")
    elif current_vice != vice:
        reasons.append(f"Vice {label(vice)}, your next best option.")
    outfield = [pid for pid in order if _position(players.get(pid, {})) != 1]
    use = result["bench_use"]
    if outfield:
        first = outfield[0]
        current_outfield = [pid for pid in benched if _position(players.get(pid, {})) != 1]
        line = "Bench order: " + ", ".join(label(pid) for pid in outfield) + "."
        if current_outfield != outfield and not promoted:
            line += f" {label(first)} first: {use.get(first, 0.0):.0%} chance he comes on."
        reasons.append(line)
    if not result["uncertain"] and not any(use.values()):
        reasons.append("No starter is flagged, so the bench order only matters if someone misses out late.")
    return {
        "picks": advised,
        "expected": result,
        "current_expected": current,
        "gain": round(result["expected_points"] - current["expected_points"], 2),
        "captain_id": captain,
        "vice_id": vice,
        "starting_ids": list(xi),
        "bench_ids": list(order),
        "promoted": promoted,
        "dropped": dropped,
        "changed": advised != normalize_picks(picks, players),
        "reasons": reasons,
    }


def captaincy_options(
    players: Mapping[int, Mapping[str, Any]],
    picks: Sequence[Mapping[str, Any]],
    model_points: Optional[Mapping[int, Mapping[str, float]]] = None,
    *,
    chip: Optional[str] = None,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """The squad's best captains, with risk, ownership and the vice fallback."""
    model_points = model_points or {}
    starters, _ = starters_and_bench(picks)
    starting = {int(pick["id"]) for pick in starters}
    current_captain = next((int(p["id"]) for p in picks if p.get("is_captain")), None)
    current_vice = next((int(p["id"]) for p in picks if p.get("is_vice_captain")), None)
    extra = 2 if chip == "3xc" else 1
    options = []
    for pick in picks:
        pid = int(pick["id"])
        player = players.get(pid, {})
        xp = float(_number(player.get("expected_points"), 0.0))
        chance = _chance(player)
        fallback = current_vice if current_vice != pid else current_captain
        if fallback is None or fallback == pid:
            others = [other for other in starting if other != pid]
            fallback = max(others, key=lambda other: _if_plays(players.get(other, {})), default=None)
        value = _armband_value(players, pid, fallback) if fallback is not None else chance * _if_plays(player)
        models = [value_ for value_ in (model_points.get(pid) or {}).values() if value_ is not None]
        spread = float(np.std(models)) if len(models) > 1 else None
        ownership = _number(player.get("selected_by_percent"), None)
        relative = (spread / xp) if spread is not None and xp > 0 else None
        risk = (
            "minutes" if chance < 1 else
            "high" if relative is not None and relative >= 0.3 else
            "medium" if relative is not None and relative >= 0.15 else
            "low" if relative is not None else "unknown"
        )
        options.append(
            {
                "id": pid,
                "web_name": _name(player, pid),
                "team_name": player.get("team_name"),
                "position": POSITION_SHORT.get(_position(player)),
                "expected_points": round(xp, 2),
                "points_if_plays": round(_if_plays(player), 2),
                "availability": round(chance, 3),
                "armband_value": round(value * extra, 2),
                "fallback_id": fallback,
                "fallback_name": _name(players.get(fallback), fallback) if fallback else None,
                "model_spread": round(spread, 3) if spread is not None else None,
                "model_min": round(min(models), 2) if models else None,
                "model_max": round(max(models), 2) if models else None,
                "risk": risk,
                "ownership": ownership,
                "ownership_tag": None if ownership is None else (
                    "template" if ownership >= 30 else "differential" if ownership < 10 else "popular"
                ),
                "fixtures": player.get("fixtures") or [],
                "opponent": player.get("opponent_team_name"),
                "was_home": player.get("was_home"),
                "starting": pid in starting,
                "current_captain": pid == current_captain,
                "current_vice": pid == current_vice,
            }
        )
    options.sort(key=lambda item: (-item["armband_value"], -item["expected_points"], item["id"]))
    return options[:limit]


# ----------------------------------------------------------------------
# News and prices
# ----------------------------------------------------------------------


def price_pressure(element: Mapping[str, Any], total_players: Optional[float]) -> Dict[str, Any]:
    """Net transfers this gameweek against estimated owners (a heuristic).

    FPL's price algorithm is not public; heavy net buying or selling relative
    to a player's owners is the usual early sign of a rise or fall.
    """
    transfers_in = int(_number(element.get("transfers_in_event"), 0) or 0)
    transfers_out = int(_number(element.get("transfers_out_event"), 0) or 0)
    net = transfers_in - transfers_out
    ownership = _number(element.get("selected_by_percent"), None)
    owners = (
        ownership / 100.0 * float(total_players)
        if ownership is not None and total_players
        else None
    )
    pressure = net / owners if owners else None
    if pressure is None:
        direction = "unknown"
    elif pressure >= PRESSURE_THRESHOLD:
        direction = "rise likely"
    elif pressure <= -PRESSURE_THRESHOLD:
        direction = "fall likely"
    else:
        direction = "steady"
    change = _number(element.get("cost_change_event"), None)
    start_change = _number(element.get("cost_change_start"), None)
    return {
        "transfers_in_event": transfers_in,
        "transfers_out_event": transfers_out,
        "net_transfers": net,
        "owners": int(owners) if owners is not None else None,
        "pressure": round(pressure, 4) if pressure is not None else None,
        "direction": direction,
        "change_this_gameweek": round(change / 10, 1) if change is not None else None,
        "change_since_start": round(start_change / 10, 1) if start_change is not None else None,
        "heuristic": True,
    }


def selling_effect(purchase: Optional[float], current: Optional[float]) -> Dict[str, Optional[float]]:
    """Selling price now, and after a £0.1m rise or fall."""
    if current is None:
        return {"now": None, "after_rise": None, "after_fall": None}
    return {
        "now": selling_price(purchase, current),
        "after_rise": selling_price(purchase, round(current + 0.1, 1)),
        "after_fall": selling_price(purchase, round(current - 0.1, 1)),
    }


# ----------------------------------------------------------------------
# Outlook: chip planner and fixture ticker
# ----------------------------------------------------------------------


def _best_week(
    squad: Sequence[Mapping[str, Any]], points: pd.DataFrame, gameweek: int
) -> Tuple[List[Dict[str, Any]], Optional[float], Optional[Dict[str, Any]], set]:
    """The squad's records, best XI total, captain and XI ids for a gameweek.

    The total and captain are None when the squad cannot field a legal XI.
    """
    records = [
        {
            "id": int(player["id"]),
            "web_name": player.get("web_name"),
            "element_type": _position(player),
            "expected_points": float(points.at[int(player["id"]), gameweek])
            if int(player["id"]) in points.index
            else 0.0,
        }
        for player in squad
    ]
    try:
        lineup, total = best_lineup(records)
    except ValueError:
        return records, None, None, set()
    captain = max(lineup, key=lambda player: player["expected_points"])
    return records, total, captain, {player["id"] for player in lineup}


def chip_values(
    squad: Sequence[Mapping[str, Any]],
    points: pd.DataFrame,
    gameweeks: Sequence[int],
    *,
    chips_played: Sequence[Mapping[str, Any]] = (),
    fixture_counts: Optional[Mapping[int, Mapping[int, int]]] = None,
    free_hit_total: Optional[Callable[[int], Optional[float]]] = None,
) -> Dict[str, Any]:
    """What each chip would add in each gameweek, for the squad as it stands.

    Bench Boost adds the bench of that week's best lineup, Triple Captain
    one more captain score, and Free Hit the best one-week squad within the
    budget minus this squad's best lineup. No further transfers are assumed.
    """
    from src.optimizer import chip_status

    fixture_counts = fixture_counts or {}
    weeks = []
    for gameweek in gameweeks:
        records, xi_total, captain, lineup_ids = _best_week(squad, points, int(gameweek))
        legal = xi_total is not None
        captain_points = captain["expected_points"] if captain else 0.0
        base = (xi_total or 0.0) + captain_points
        bench = sum(player["expected_points"] for player in records if player["id"] not in lineup_ids)
        free_hit = free_hit_total(int(gameweek)) if free_hit_total and legal else None
        counts = [int((fixture_counts.get(int(player["id"])) or {}).get(int(gameweek), 1)) for player in squad]
        status = chip_status(chips_played, int(gameweek))
        weeks.append(
            {
                "gameweek": int(gameweek),
                "half": status["half"],
                "base": round(base, 2) if legal else None,
                "captain_id": captain["id"] if captain else None,
                "captain_name": captain.get("web_name") if captain else None,
                "bboost": round(bench, 2) if legal else None,
                "3xc": round(captain_points, 2) if legal else None,
                "freehit": round(max(0.0, free_hit - base), 2) if free_hit is not None else None,
                "freehit_total": round(free_hit, 2) if free_hit is not None else None,
                "doubles": sum(1 for count in counts if count >= 2),
                "blanks": sum(1 for count in counts if count == 0),
                "available": {
                    chip: bool(value.get("available_this_half"))
                    for chip, value in status["chips"].items()
                },
            }
        )
    best: Dict[str, Optional[int]] = {}
    for chip in ("bboost", "3xc", "freehit"):
        options = [week for week in weeks if week[chip] is not None and week["available"].get(chip, True)]
        best[chip] = max(options, key=lambda week: (week[chip], -week["gameweek"]))["gameweek"] if options else None
    return {"gameweeks": weeks, "best": best, "assumption": "Your squad as it stands, with no further transfers."}


def fixture_ticker(
    squad: Sequence[Mapping[str, Any]],
    points: pd.DataFrame,
    gameweeks: Sequence[int],
    team_fixtures: Mapping[int, Mapping[int, Sequence[Mapping[str, Any]]]],
) -> Dict[str, Any]:
    """Each squad player's fixtures and projected points, gameweek by gameweek."""
    rows = []
    for player in squad:
        player_id = int(player["id"])
        team_id = player.get("team_id")
        by_week = team_fixtures.get(int(team_id), {}) if team_id is not None else {}
        cells = []
        for gameweek in gameweeks:
            value = float(points.at[player_id, gameweek]) if player_id in points.index else 0.0
            cells.append(
                {
                    "gameweek": int(gameweek),
                    "fixtures": [
                        {
                            "opponent": fixture.get("opponent_short") or fixture.get("opponent"),
                            "home": fixture.get("home"),
                            "difficulty": fixture.get("difficulty"),
                        }
                        for fixture in by_week.get(int(gameweek), [])
                    ],
                    "expected_points": round(value, 2),
                }
            )
        difficulties = [
            float(fixture["difficulty"])
            for cell in cells
            for fixture in cell["fixtures"]
            if fixture.get("difficulty")
        ]
        rows.append(
            {
                "id": player_id,
                "web_name": player.get("web_name"),
                "team_name": player.get("team_name"),
                "position": POSITION_SHORT.get(_position(player)),
                "pick_position": player.get("pick_position"),
                "cells": cells,
                "total": round(sum(cell["expected_points"] for cell in cells), 2),
                "difficulty": round(float(np.mean(difficulties)), 2) if difficulties else None,
            }
        )
    totals = []
    for gameweek in gameweeks:
        _, xi_total, captain, _ = _best_week(squad, points, int(gameweek))
        totals.append(
            {
                "gameweek": int(gameweek),
                "best_xi": round(xi_total + captain["expected_points"], 2)
                if xi_total is not None and captain
                else None,
            }
        )
    return {"gameweeks": [int(value) for value in gameweeks], "rows": rows, "totals": totals}


# ----------------------------------------------------------------------
# Gameweek review: how the manager's decisions did
# ----------------------------------------------------------------------


def gameweek_review(
    picks_payload: Mapping[str, Any],
    players: Mapping[int, Mapping[str, Any]],
    actuals: Mapping[int, Mapping[str, float]],
    *,
    forecast: Optional[Mapping[int, float]] = None,
    transfers: Sequence[Mapping[str, Any]] = (),
    since: Optional[Mapping[int, float]] = None,
    finished: bool = True,
) -> Dict[str, Any]:
    """Captain, bench, lineup and transfers of one gameweek against hindsight."""
    chip = picks_payload.get("active_chip")
    picks = sorted(picks_payload.get("picks", []), key=lambda pick: int(pick.get("position") or 0))
    entry = picks_payload.get("entry_history") or {}
    multiplier = 3 if chip == "3xc" else 2

    def points_of(player_id: int) -> float:
        return float(_number((actuals.get(player_id) or {}).get("points"), 0.0))

    def minutes_of(player_id: int) -> float:
        return float(_number((actuals.get(player_id) or {}).get("minutes"), 0.0))

    def label(player_id: Optional[int]) -> Optional[str]:
        return None if player_id is None else _name(players.get(player_id), player_id)

    ids = [int(pick["element"]) for pick in picks]
    starters = [int(pick["element"]) for pick in picks if int(pick.get("position") or 0) <= STARTERS]
    final = list(ids) if chip == "bboost" else list(starters)
    subs = []
    for sub in picks_payload.get("automatic_subs", []) or []:
        incoming, outgoing = int(sub.get("element_in")), int(sub.get("element_out"))
        if outgoing in final:
            final[final.index(outgoing)] = incoming
        subs.append({"in_id": incoming, "in_name": label(incoming), "out_id": outgoing, "out_name": label(outgoing)})
    captain = next((int(p["element"]) for p in picks if p.get("is_captain")), None)
    vice = next((int(p["element"]) for p in picks if p.get("is_vice_captain")), None)
    armband = None
    if captain in final and minutes_of(captain) > 0:
        armband = captain
    elif vice in final and vice is not None and minutes_of(vice) > 0:
        armband = vice
    computed = sum(points_of(pid) for pid in final) + (
        (multiplier - 1) * points_of(armband) if armband is not None else 0.0
    )
    official = _number(entry.get("points"), None)
    bench_ids = [pid for pid in ids if pid not in final]
    bench_points = sum(points_of(pid) for pid in bench_ids)
    played = [pid for pid in final if minutes_of(pid) > 0]
    best_captain = max(played, key=points_of, default=None)
    captain_cost = (
        (multiplier - 1) * (points_of(best_captain) - (points_of(armband) if armband is not None else 0.0))
        if best_captain is not None
        else 0.0
    )

    records = [
        {"id": pid, "web_name": label(pid), "element_type": _position(players.get(pid, {})), "expected_points": points_of(pid)}
        for pid in ids
    ]
    hindsight = None
    try:
        lineup, total = best_lineup(records)
        top = max(lineup, key=lambda player: player["expected_points"])
        hindsight_total = total + (multiplier - 1) * top["expected_points"]
        if chip == "bboost":
            hindsight_total = sum(points_of(pid) for pid in ids) + (multiplier - 1) * max(points_of(pid) for pid in ids)
        hindsight = {
            "total": round(hindsight_total, 1),
            "starting_ids": [player["id"] for player in lineup],
            "captain_id": top["id"],
            "captain_name": top["web_name"],
            "left_on_table": round(hindsight_total - computed, 1),
        }
    except ValueError:
        pass

    hits = int(_number(entry.get("event_transfers_cost"), 0) or 0)
    items = []
    for transfer in transfers:
        incoming = int(transfer.get("element_in_id", transfer.get("element_in")) or 0)
        outgoing = int(transfer.get("element_out_id", transfer.get("element_out")) or 0)
        item = {
            "in_id": incoming,
            "in_name": label(incoming),
            "out_id": outgoing,
            "out_name": label(outgoing),
            "in_cost": transfer.get("element_in_cost"),
            "out_cost": transfer.get("element_out_cost"),
            "in_points": points_of(incoming),
            "out_points": points_of(outgoing),
            "delta": points_of(incoming) - points_of(outgoing),
            "in_played": incoming in final,
        }
        if since is not None and incoming in since and outgoing in since:
            item["since_delta"] = float(since[incoming]) - float(since[outgoing])
        items.append(item)
    net = sum(item["delta"] for item in items) - hits
    since_net = (
        sum(item.get("since_delta", item["delta"]) for item in items) - hits if items else None
    )
    transfer_summary = {
        "items": items,
        "count": len(items),
        "hits": hits,
        "net": round(net, 1),
        "since_net": round(since_net, 1) if since_net is not None else None,
        "verdict": None if not items else (
            "paid off" if (since_net if since_net is not None else net) > 0
            else "not yet" if (since_net if since_net is not None else net) < 0 else "even"
        ),
        "free_hit": chip == "freehit",
    }

    forecast_view: Dict[str, Any] = {"available": forecast is not None}
    if forecast is not None:
        rows = []
        for pid in starters:
            expected = float(_number(forecast.get(pid), 0.0))
            rows.append(
                {
                    "id": pid,
                    "web_name": label(pid),
                    "expected_points": round(expected, 2),
                    "points": points_of(pid),
                    "difference": round(points_of(pid) - expected, 2),
                }
            )
        rows.sort(key=lambda row: row["difference"])
        captain_expected = float(_number(forecast.get(captain), 0.0)) if captain else 0.0
        forecast_view.update(
            {
                "xi_expected": round(sum(row["expected_points"] for row in rows) + (multiplier - 1) * captain_expected, 2),
                "xi_points": round(sum(row["points"] for row in rows) + (multiplier - 1) * (points_of(captain) if captain else 0.0), 1),
                "players": rows,
                "under": rows[:3],
                "over": list(reversed(rows[-3:])),
            }
        )
    return {
        "gameweek": picks_payload.get("gameweek"),
        "finished": bool(finished),
        "provisional": not finished,
        "chip": chip,
        "points": {
            "official": official,
            "computed": round(computed, 1),
            "matches": official is None or abs(float(official) - computed) < 0.5,
            "hits": hits,
            "net": round((official if official is not None else computed) - hits, 1),
        },
        "bench": {
            "official": _number(entry.get("points_on_bench"), None),
            "computed": round(bench_points, 1),
            "ids": bench_ids,
        },
        "captain": {
            "id": captain,
            "name": label(captain),
            "points": points_of(captain) if captain else None,
            "armband_id": armband,
            "armband_name": label(armband),
            "multiplier": multiplier,
            "best_id": best_captain,
            "best_name": label(best_captain),
            "best_points": points_of(best_captain) if best_captain is not None else None,
            "cost": round(max(0.0, captain_cost), 1),
        },
        "autosubs": subs,
        "hindsight": hindsight,
        "transfers": transfer_summary,
        "forecast": forecast_view,
        "players": [
            {
                "id": pid,
                "web_name": label(pid),
                "team_name": players.get(pid, {}).get("team_name"),
                "element_type": _position(players.get(pid, {})),
                "position": POSITION_SHORT.get(_position(players.get(pid, {}))),
                "pick_position": index + 1,
                "points": points_of(pid),
                "minutes": minutes_of(pid),
                "in_final_xi": pid in final,
                "role": "captain" if pid == captain else "vice" if pid == vice else "",
                "expected_points": round(float(_number(forecast.get(pid), 0.0)), 2) if forecast is not None else None,
            }
            for index, pid in enumerate(ids)
        ],
    }

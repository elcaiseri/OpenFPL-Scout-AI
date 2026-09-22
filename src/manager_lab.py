"""Owner-linked FPL entry review and transfer planning.

Public FPL endpoints expose a manager's picks, per-gameweek bank and transfer
counts, but never per-player selling prices or the current free-transfer bank.
Both are estimated here, labelled as estimates, and overridable by the owner.
Nothing derived from them is presented as an official figure.

Reviewing an entry never runs inference. Planning transfers does, because it
needs a forecast for a gameweek whose deadline has not passed.
"""

from __future__ import annotations

from collections import Counter
from math import floor
from typing import Any, Iterable, Mapping, Sequence

from src.observatory import best_xi, numeric

POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_SHAPE = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
SQUAD_SIZE = sum(SQUAD_SHAPE.values())
MAX_PER_CLUB = 3
HIT_COST = 4
MAX_FREE_TRANSFERS = 5
UNAVAILABLE = {"i", "s", "u", "n"}

SELLING_PRICE_NOTE = (
    "Selling prices are not public. Every sale is valued at the player's current "
    "price, which may overestimate the funds available from sales. "
    "Confirm affordability in the official game."
)
PLAN_NOTE = (
    "One-transfer plans are searched exhaustively. Two- and three-transfer plans "
    "use a beam search over the strongest single moves: they are strong "
    "candidates, not proven optima."
)


def _position(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value in SQUAD_SHAPE:
            return value
        code = numeric(value)
        if code is not None and int(code) in POSITIONS:
            return POSITIONS[int(code)]
    return None


def xi_value(squad: Sequence[Mapping], field: str = "expected_points"):
    """Return the best legal XI, its captain, and the captain-doubled total."""
    scored = [p for p in squad if numeric(p.get(field)) is not None]
    xi = best_xi(scored, field)
    if len(xi) != 11:
        return None, [], None
    captain = max(xi, key=lambda p: (p[field], -p["id"]))
    return sum(p[field] for p in xi) + captain[field], xi, captain


def _clubs_legal(squad: Iterable[Mapping]) -> bool:
    counts = Counter(p.get("team") for p in squad)
    return not counts or max(counts.values()) <= MAX_PER_CLUB


def estimate_free_transfers(history: Mapping, upcoming_gameweek: int) -> dict:
    """Replay public transfer counts to estimate the banked free transfers.

    Chip weeks are read from the entry's chip history. This cannot see a
    transfer made and reversed inside one gameweek, so it is an estimate.
    """
    events = {}
    for row in history.get("current", []) or []:
        gw = numeric(row.get("event"))
        if gw is not None:
            events[int(gw)] = row
    chips = {}
    for chip in history.get("chips", []) or []:
        gw = numeric(chip.get("event"))
        if gw is not None:
            chips[int(gw)] = str(chip.get("name", "")).lower()
    played = sorted(gw for gw in events if gw < upcoming_gameweek)
    if not played:
        return {"free_transfers": 1, "basis": "no-completed-gameweeks", "chip_gameweeks": []}
    free = 1
    used_chips = []
    for gw in played:
        if gw == min(played):
            continue
        chip = chips.get(gw, "")
        if chip in {"wildcard", "freehit"}:
            used_chips.append(gw)
        else:
            made = numeric(events[gw].get("event_transfers")) or 0
            free = max(0, free - int(made))
        free = min(MAX_FREE_TRANSFERS, free + 1)
    return {
        "free_transfers": free,
        "basis": "derived-from-public-transfer-history",
        "chip_gameweeks": used_chips,
    }


def review(picks: Mapping, week: Mapping | None, entry: Mapping | None = None) -> dict:
    """Join a manager's official picks with our forecast and the official result."""
    forecast = {p["id"]: p for p in (week or {}).get("players", [])}
    squad = []
    for pick in picks.get("picks", []) or []:
        player_id = numeric(pick.get("element"))
        if player_id is None:
            continue
        player_id = int(player_id)
        ours = forecast.get(player_id, {})
        player = pick.get("player") or {}
        multiplier = int(numeric(pick.get("multiplier")) or 0)
        squad.append({
            "id": player_id,
            "name": ours.get("name") or player.get("web_name") or str(player_id),
            "team": ours.get("team") or player.get("team_name") or "—",
            "position": _position(pick.get("element_type"), player.get("element_type"), ours.get("position")) or "—",
            "expected_points": ours.get("expected_points"),
            "actual_points": ours.get("actual_points"),
            "price": ours.get("price") if ours.get("price") is not None else numeric(player.get("price")),
            "multiplier": multiplier,
            "started": multiplier > 0,
            "is_captain": bool(pick.get("is_captain")),
            "is_vice_captain": bool(pick.get("is_vice_captain")),
            "opponent": ours.get("opponent"),
            "in_our_squad": bool(ours.get("in_squad")),
            "our_role": ours.get("role", ""),
        })
    started = [p for p in squad if p["started"]]
    bench = [p for p in squad if not p["started"]]
    captain = next((p for p in squad if p["is_captain"]), None)
    scored = [p for p in started if p["actual_points"] is not None]
    complete = len(scored) == len(started) and bool(started)
    predicted = [p for p in started if p["expected_points"] is not None]
    entry_history = picks.get("entry_history", {}) or {}
    ours = (week or {}).get("squad") or {}
    overlap = sorted({p["id"] for p in squad} & {p["id"] for p in (week or {}).get("players", []) if p.get("in_squad")})
    return {
        "gameweek": picks.get("gameweek"),
        "squad": squad,
        "starting": started,
        "bench": bench,
        "captain": captain,
        "vice_captain": next((p for p in squad if p["is_vice_captain"]), None),
        "active_chip": picks.get("active_chip"),
        "automatic_subs": picks.get("automatic_subs", []),
        # Official figures, taken as reported. Never recomputed from our data.
        "official_points": numeric(entry_history.get("points")),
        "official_total_points": numeric(entry_history.get("total_points")),
        "official_rank": numeric(entry_history.get("rank")),
        "official_overall_rank": numeric(entry_history.get("overall_rank")),
        "bench_points": numeric(entry_history.get("points_on_bench")),
        "transfers_made": numeric(entry_history.get("event_transfers")),
        "transfers_cost": numeric(entry_history.get("event_transfers_cost")),
        "bank": numeric(entry_history.get("bank")) / 10 if numeric(entry_history.get("bank")) is not None else None,
        "squad_value": numeric(entry_history.get("value")) / 10 if numeric(entry_history.get("value")) is not None else None,
        # Our view of the same picks. Missing forecasts stay missing.
        "predicted_points": sum(p["expected_points"] * max(1, p["multiplier"]) for p in predicted) if len(predicted) == len(started) and started else None,
        "actual_points": sum(p["actual_points"] * max(1, p["multiplier"]) for p in scored) if complete else None,
        "matched_forecasts": len(predicted),
        "matched_actuals": len(scored),
        "our_squad_overlap": len(overlap),
        "our_predicted_points": ours.get("expected_points"),
        "our_actual_points": (week or {}).get("analysis", {}).get("squad", {}).get("actual_points"),
        "note": (
            "Official points, rank, bench and transfer costs are reported by FPL. "
            "Our predicted total applies the official multipliers to the same picks; "
            "it is not an alternative score."
        ),
    }


def _official_index(bootstrap: Mapping):
    """Index the official bootstrap by player ID, with club names resolved."""
    teams = {}
    for team in bootstrap.get("teams", []) or []:
        code = numeric(team.get("id"))
        if code is not None:
            teams[int(code)] = team.get("name")
    elements = {}
    for element in bootstrap.get("elements", []) or []:
        code = numeric(element.get("id"))
        if code is not None:
            elements[int(code)] = element
    return teams, elements


def _merge(player_id, row, element, teams):
    """Build one candidate: forecast points, but always current price and status."""
    row, element = row or {}, element or {}
    price = numeric(element.get("now_cost"))
    position = _position(row.get("element_type"), row.get("position"), element.get("element_type"))
    if position is None:
        return None
    team_id = numeric(element.get("team"))
    return {
        "id": player_id,
        "name": row.get("web_name") or row.get("name") or element.get("web_name") or str(player_id),
        "team": (teams.get(int(team_id)) if team_id is not None else None) or row.get("team_name") or row.get("team") or "—",
        "position": position,
        "expected_points": numeric(row.get("expected_points")),
        # Prices and availability are always read live: an archived forecast's
        # captured price is stale the moment the official game revalues a player.
        "price": price / 10 if price is not None else None,
        "status": str(element.get("status") or "a"),
        "news": element.get("news") or None,
        "opponent": row.get("opponent_team_name") or row.get("opponent"),
    }


def catalog(predictions: Iterable[Mapping], bootstrap: Mapping) -> dict[int, dict]:
    """Merge a forecast with official prices and availability, keyed by player ID.

    Accepts either raw inference rows or archived dashboard players.
    """
    teams, elements = _official_index(bootstrap)
    players = {}
    for row in predictions:
        code = numeric(row.get("id"))
        if code is None or numeric(row.get("expected_points")) is None:
            continue
        player_id = int(code)
        merged = _merge(player_id, row, elements.get(player_id), teams)
        if merged is not None:
            players[player_id] = merged
    return players


def squad_from_picks(picks: Mapping, players: Mapping[int, Mapping], bootstrap: Mapping):
    """Resolve 15 official picks against the forecast, reporting any without one."""
    teams, elements = _official_index(bootstrap)
    squad, unforecast, unresolved = [], [], []
    for pick in picks.get("picks", []) or []:
        code = numeric(pick.get("element"))
        if code is None:
            continue
        player_id = int(code)
        entry = players.get(player_id)
        if entry is None:
            entry = _merge(player_id, {"element_type": pick.get("element_type")}, elements.get(player_id), teams)
            if entry is None:
                unresolved.append(player_id)
                continue
            unforecast.append(entry["name"])
        squad.append(dict(entry))
    return squad, unforecast, unresolved


def optimize(
    squad: Sequence[Mapping],
    pool: Iterable[Mapping],
    *,
    bank: float,
    free_transfers: int,
    max_transfers: int = 3,
    wildcard: bool = False,
    width: int = 20,
    beam: int = 60,
) -> dict:
    """Search transfer plans that raise the predicted captain-doubled XI score.

    One transfer is searched exhaustively. Deeper plans extend the strongest
    shallower plans, so they are strong candidates rather than proven optima.
    Sales are valued at current price; see SELLING_PRICE_NOTE.
    """
    squad = [dict(p) for p in squad]
    if len(squad) != SQUAD_SIZE or Counter(p["position"] for p in squad) != Counter(SQUAD_SHAPE):
        raise ValueError("A transfer plan needs a complete 15-player squad")
    if any(numeric(p.get("price")) is None for p in squad):
        raise ValueError("Every squad player needs a current price")
    baseline, base_xi, base_captain = xi_value(squad)
    if baseline is None:
        raise ValueError("The squad has no complete predicted XI")
    if wildcard:
        return _optimize_wildcard(squad, pool, bank=bank, free_transfers=free_transfers)

    held = {p["id"] for p in squad}
    by_position: dict[str, list[dict]] = {position: [] for position in SQUAD_SHAPE}
    for player in pool:
        if player["id"] in held or player["position"] not in by_position:
            continue
        if numeric(player.get("price")) is None or player.get("status") in UNAVAILABLE:
            continue
        by_position[player["position"]].append(player)
    for group in by_position.values():
        group.sort(key=lambda p: (-p["expected_points"], p["id"]))

    plans: dict[int, dict] = {}
    states = [{"outs": [], "ins": [], "squad": squad, "bank": float(bank), "score": baseline}]
    seen = {frozenset(held)}
    for depth in range(1, max_transfers + 1):
        # Depth 1 sees every state; deeper searches extend only the best so far.
        found = []
        for state in states:
            for out in state["squad"]:
                remaining = [p for p in state["squad"] if p["id"] != out["id"]]
                funds = state["bank"] + out["price"]
                limit = len(by_position[out["position"]]) if depth == 1 else width
                for incoming in by_position[out["position"]][:limit]:
                    if incoming["price"] > funds + 1e-9:
                        continue
                    if any(p["id"] == incoming["id"] for p in state["ins"]):
                        continue
                    candidate = remaining + [incoming]
                    signature = frozenset(p["id"] for p in candidate)
                    if signature in seen or not _clubs_legal(candidate):
                        continue
                    score, _, _ = xi_value(candidate)
                    if score is None:
                        continue
                    seen.add(signature)
                    found.append({
                        "outs": state["outs"] + [out],
                        "ins": state["ins"] + [incoming],
                        "squad": candidate,
                        "bank": funds - incoming["price"],
                        "score": score,
                    })
        if not found:
            break
        found.sort(key=lambda s: -s["score"])
        hits = max(0, depth - max(0, int(free_transfers)))
        best = found[0]
        plans[depth] = {
            "transfers": depth,
            "hits": hits,
            "hit_cost": hits * HIT_COST,
            "gain": best["score"] - baseline,
            "net_gain": best["score"] - baseline - hits * HIT_COST,
            "predicted_points": best["score"],
            "net_predicted_points": best["score"] - hits * HIT_COST,
            "remaining_bank": best["bank"],
            "moves": [
                {"out": _brief(o), "in": _brief(i), "cost": i["price"] - o["price"],
                 "gain": i["expected_points"] - o["expected_points"] if numeric(o.get("expected_points")) is not None else None}
                for o, i in zip(best["outs"], best["ins"])
            ],
            "exhaustive": depth == 1,
            **_shape(best["squad"]),
        }
        states = found[:beam]

    worthwhile = [p for p in plans.values() if p["net_gain"] > 1e-9]
    return {
        "baseline": {
            "predicted_points": baseline,
            "captain": _brief(base_captain) if base_captain else None,
            **_shape(squad),
        },
        "bank": float(bank),
        "free_transfers": int(free_transfers),
        "wildcard": False,
        "plans": [plans[k] for k in sorted(plans)],
        "recommended": max(worthwhile, key=lambda p: p["net_gain"]) if worthwhile else None,
        "notes": [SELLING_PRICE_NOTE, PLAN_NOTE],
    }


def _optimize_wildcard(squad, pool, *, bank, free_transfers):
    """Select the whole squad, XI and captain jointly, without a transfer cap.

    Binary variables represent squad membership, starting and captaincy. The
    budget constrains the final squad, allowing simultaneous sales and buys.
    """
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import lil_matrix

    held = {p["id"] for p in squad}
    candidates = {p["id"]: dict(p) for p in squad}
    for p in pool:
        if (p["id"] not in held and p.get("position") in SQUAD_SHAPE
                and numeric(p.get("price")) is not None
                and numeric(p.get("expected_points")) is not None
                and p.get("status") not in UNAVAILABLE):
            candidates[p["id"]] = dict(p)
    players = sorted(candidates.values(), key=lambda p: p["id"])
    count = len(players)
    prices = np.array([round(p["price"] * 10) for p in players])
    funds = sum(p["price"] for p in squad) + bank
    budget = floor(funds * 10 + 1e-7)
    points = np.array([numeric(p.get("expected_points")) or 0 for p in players])
    objective = np.concatenate((np.zeros(count), -points, -points))
    upper = np.ones(3 * count)
    for i, p in enumerate(players):
        if numeric(p.get("expected_points")) is None:
            upper[count + i] = upper[2 * count + i] = 0
    rows, lower, limits = [], [], []

    def constrain(coefficients, low, high):
        rows.append(coefficients)
        lower.append(low)
        limits.append(high)

    constrain(dict(enumerate(prices)), -np.inf, budget)
    for position, size in SQUAD_SHAPE.items():
        indices = [i for i, p in enumerate(players) if p["position"] == position]
        constrain({i: 1 for i in indices}, size, size)
        min_xi, max_xi = {"GK": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}[position]
        constrain({count + i: 1 for i in indices}, min_xi, max_xi)
    for club in sorted({p["team"] for p in players}):
        constrain({i: 1 for i, p in enumerate(players) if p["team"] == club}, 0, MAX_PER_CLUB)
    constrain({count + i: 1 for i in range(count)}, 11, 11)
    constrain({2 * count + i: 1 for i in range(count)}, 1, 1)
    for i in range(count):
        constrain({count + i: 1, i: -1}, -np.inf, 0)
        constrain({2 * count + i: 1, count + i: -1}, -np.inf, 0)
    matrix = lil_matrix((len(rows), 3 * count))
    for row, coefficients in enumerate(rows):
        for col, value in coefficients.items():
            matrix[row, col] = value
    constraints = LinearConstraint(matrix.tocsr(), lower, limits)
    result = milp(objective, integrality=np.ones(3 * count),
                  bounds=Bounds(0, upper), constraints=constraints,
                  options={"time_limit": 30, "mip_rel_gap": 0})
    if result.x is None:
        raise ValueError("No legal wildcard squad was found within the budget and search time limit.")
    optimal = result.status == 0
    # Among equally strong XIs, prefer keeping existing players to avoid
    # suggesting unnecessary bench transfers just because they are free.
    if optimal:
        tie = milp(np.concatenate(([0 if p["id"] in held else 1 for p in players], np.zeros(2 * count))),
                   integrality=np.ones(3 * count), bounds=Bounds(0, upper),
                   constraints=[constraints, LinearConstraint(objective, -np.inf, result.fun + 1e-7)],
                   options={"time_limit": 5, "mip_rel_gap": 0})
        if tie.x is not None:
            result = tie
    selected = np.rint(result.x)
    values = constraints.A @ selected
    if (np.any(np.abs(result.x - selected) > 1e-5)
            or np.any(selected < 0) or np.any(selected > upper)
            or np.any(values < np.array(lower) - 1e-6)
            or np.any(values > np.array(limits) + 1e-6)):
        raise ValueError("The wildcard search did not return a valid squad; try again.")
    chosen = [p for i, p in enumerate(players) if selected[i]]
    chosen_ids = {p["id"] for p in chosen}
    outs = sorted((p for p in squad if p["id"] not in chosen_ids), key=lambda p: (p["position"], p["id"]))
    ins = sorted((p for p in chosen if p["id"] not in held), key=lambda p: (p["position"], p["id"]))
    baseline, _, captain = xi_value(squad)
    score, _, _ = xi_value(chosen)
    plan = {
        "transfers": len(ins), "hits": 0, "hit_cost": 0,
        "gain": score - baseline, "net_gain": score - baseline,
        "predicted_points": score, "net_predicted_points": score,
        "remaining_bank": round(funds - sum(p["price"] for p in chosen), 10),
        "moves": [{"out": _brief(o), "in": _brief(i), "cost": i["price"] - o["price"],
                   "gain": i["expected_points"] - o["expected_points"] if numeric(o.get("expected_points")) is not None else None}
                  for o, i in zip(outs, ins)],
        "exhaustive": optimal, "search_method": "integer-optimization",
        **_shape(chosen),
    }
    return {
        "baseline": {"predicted_points": baseline, "captain": _brief(captain), **_shape(squad)},
        "bank": float(bank), "free_transfers": int(free_transfers), "wildcard": True,
        "plans": [plan], "recommended": plan if plan["net_gain"] > 1e-9 else None,
        "notes": [SELLING_PRICE_NOTE,
                  "Wildcard planning allows up to 15 transfers with no points hits. The bank, squad positions, legal XI and three-player club limit still apply. This does not activate a chip in FPL.",
                  "The full squad was optimized for the captain-doubled predicted XI."
                  if optimal else "The search reached its time limit; this is a legal candidate, not a proven optimum."],
    }


def _brief(player: Mapping) -> dict:
    return {k: player.get(k) for k in ("id", "name", "team", "position", "expected_points", "price", "status", "opponent")}


def _shape(squad: Sequence[Mapping]) -> dict:
    score, xi, captain = xi_value(squad)
    xi_ids = {p["id"] for p in xi}
    return {
        "xi": [{**_brief(p), "is_captain": bool(captain and p["id"] == captain["id"])} for p in xi],
        "bench": [_brief(p) for p in squad if p["id"] not in xi_ids],
        "squad_value": sum(p["price"] for p in squad) if all(numeric(p.get("price")) is not None for p in squad) else None,
    }

"""Squad optimisation for the owner dashboard: transfers, Wildcard, Free Hit.

The optimiser plans only; it never changes a manager's team. It maximises
projected points of each gameweek's best legal XI plus captain over a
horizon of gameweeks, within the budget, the positional quotas and the
three-per-club rule, minus four points for each transfer beyond the free
ones. The exact optimum comes from scipy's mixed-integer solver (HiGHS).

Official FPL does not publish a manager's free transfers or selling prices,
so both are estimated from public history; the dashboard shows the estimate
and lets the owner correct it.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

SQUAD_QUOTAS = {1: 2, 2: 5, 3: 5, 4: 3}
STARTER_MIN = {1: 1, 2: 3, 3: 2, 4: 1}
STARTER_MAX = {1: 1, 2: 5, 3: 5, 4: 3}
POSITION_SHORT = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
POSITION_NAMES = {1: "Goalkeeper", 2: "Defender", 3: "Midfielder", 4: "Forward"}
SQUAD_SIZE = 15
STARTERS = 11
MAX_BANKED_TRANSFERS = 5
HIT_COST = 4.0
FREE_TRANSFER_CHIPS = {"wildcard", "freehit"}
MODES = {"transfers", "wildcard", "freehit"}
# Chips come twice a season since 2025/26: one of each per half.
SEASON_HALVES = ((1, 19), (20, 38))


# ----------------------------------------------------------------------
# Estimates from public data
# ----------------------------------------------------------------------


def estimate_free_transfers(
    history_rows: Iterable[Mapping[str, Any]],
    chips: Iterable[Mapping[str, Any]],
    started_event: Optional[int],
    next_gameweek: int,
    max_banked: int = MAX_BANKED_TRANSFERS,
) -> Dict[str, Any]:
    """Estimate the free transfers available for ``next_gameweek``.

    Rules applied: none are needed before the first deadline (the squad is
    picked freely); one free transfer after it, one more each gameweek, at
    most ``max_banked`` banked; transfers paid for with hits
    (``event_transfers_cost`` / 4) do not use free ones; a Wildcard or Free
    Hit week keeps the banked transfers but adds none. One-off top-ups FPL sometimes
    grants are not visible in public data.
    """
    rows = {
        int(row["event"]): row
        for row in history_rows or []
        if row.get("event") is not None
    }
    chip_weeks = {
        int(chip["event"]): str(chip.get("name") or "")
        for chip in chips or []
        if chip.get("event") is not None
    }
    first = int(started_event) if started_event else (min(rows) if rows else None)
    if first is None or next_gameweek <= first:
        return {
            "free_transfers": None,
            "unlimited": True,
            "trail": [],
            "max_banked": max_banked,
            "note": "No deadline has passed for this team yet: the squad can be changed freely.",
        }
    free = 1
    trail: List[Dict[str, Any]] = [
        {"gameweek": first, "note": "First squad picked", "available_next": free}
    ]
    for gameweek in range(first + 1, int(next_gameweek)):
        row = rows.get(gameweek, {})
        made = int(row.get("event_transfers") or 0)
        hits = int(row.get("event_transfers_cost") or 0) // int(HIT_COST)
        chip = chip_weeks.get(gameweek)
        available = free
        if chip in FREE_TRANSFER_CHIPS:
            used = 0
            note = f"{chip} played: banked transfers kept, none added"
        else:
            used = min(available, max(0, made - hits))
            note = (
                f"{made} transfer{'s' if made != 1 else ''}"
                + (f", {hits} hit{'s' if hits != 1 else ''}" if hits else "")
                if made
                else "rolled"
            )
        # A Wildcard or Free Hit week keeps the banked transfers but adds no
        # new one; every other week adds one, up to the bank's limit.
        free = available if chip in FREE_TRANSFER_CHIPS else min(max_banked, available - used + 1)
        trail.append(
            {
                "gameweek": gameweek,
                "available": available,
                "made": made,
                "hits": hits,
                "chip": chip,
                "note": note,
                "available_next": free,
            }
        )
    return {
        "free_transfers": free,
        "unlimited": False,
        "trail": trail,
        "max_banked": max_banked,
        "note": (
            "Estimated from public history: 1 free transfer a gameweek, at most "
            f"{max_banked} banked; a Wildcard or Free Hit week keeps the banked "
            "transfers without adding one. Correct it if FPL shows a different number."
        ),
    }


def selling_price(purchase: Optional[float], current: Optional[float]) -> Optional[float]:
    """FPL's sell-on rule: half of any rise (rounded down to £0.1m) is kept."""
    if current is None:
        return purchase
    if purchase is None:
        return current
    bought = int(round(float(purchase) * 10))
    now = int(round(float(current) * 10))
    if now <= bought:
        return now / 10
    return (bought + (now - bought) // 2) / 10


def purchase_prices(
    squad_ids: Iterable[int],
    transfers: Iterable[Mapping[str, Any]],
    chips: Iterable[Mapping[str, Any]],
    started_event: Optional[int],
    start_prices: Mapping[int, Optional[float]],
) -> Dict[int, Dict[str, Any]]:
    """Each squad player's estimated purchase price and where it came from.

    The latest transfer in of the player, ignoring Free Hit weeks (that
    squad reverts); otherwise his price in the manager's first gameweek;
    otherwise unknown.
    """
    free_hit_weeks = {
        int(chip["event"])
        for chip in chips or []
        if chip.get("name") == "freehit" and chip.get("event") is not None
    }
    latest: Dict[int, Tuple[float, int]] = {}
    ordered = sorted(
        transfers or [],
        key=lambda item: (int(item.get("event") or 0), str(item.get("time") or "")),
    )
    for transfer in ordered:
        event = int(transfer.get("event") or 0)
        if event in free_hit_weeks:
            continue
        player = transfer.get("element_in_id", transfer.get("element_in"))
        cost = transfer.get("element_in_cost")
        if player is None or cost is None:
            continue
        latest[int(player)] = (float(cost), event)
    result: Dict[int, Dict[str, Any]] = {}
    for player_id in squad_ids:
        player_id = int(player_id)
        if player_id in latest:
            price, event = latest[player_id]
            result[player_id] = {"price": price, "source": f"bought for GW{event}"}
        elif start_prices.get(player_id) is not None:
            result[player_id] = {
                "price": float(start_prices[player_id]),
                "source": f"price in GW{started_event}" if started_event else "first price",
            }
        else:
            result[player_id] = {"price": None, "source": "unknown: current price used"}
    return result


def chip_status(chips: Iterable[Mapping[str, Any]], gameweek: int) -> Dict[str, Any]:
    """Chips played, and whether each is still available this half (estimate)."""
    half = next(
        (index for index, (start, end) in enumerate(SEASON_HALVES) if start <= gameweek <= end),
        len(SEASON_HALVES) - 1,
    )
    start, end = SEASON_HALVES[half]
    status: Dict[str, Any] = {}
    for name in ("wildcard", "freehit", "bboost", "3xc"):
        played = sorted(
            int(chip["event"])
            for chip in chips or []
            if chip.get("name") == name and chip.get("event") is not None
        )
        status[name] = {
            "played": played,
            "available_this_half": not any(start <= event <= end for event in played),
        }
    return {"half": f"GW{start}–{end}", "chips": status}


# ----------------------------------------------------------------------
# Projections over a horizon
# ----------------------------------------------------------------------


def horizon_points(
    predict: Callable[[int], pd.DataFrame], gameweeks: Sequence[int]
) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    """Projected points per player (rows) and gameweek (columns).

    After the first gameweek a doubtful player's discount is removed (FPL's
    chance of playing describes the next round only), while players ruled
    out (factor 0) stay at zero. Returns the points, the identity of every
    projected player, and notes for gameweeks that could not be projected.
    The points' ``attrs["factors"]`` hold the availability factor each
    value includes.
    """
    columns: Dict[int, pd.Series] = {}
    factor_columns: Dict[int, pd.Series] = {}
    identities: List[pd.DataFrame] = []
    notes: List[str] = []
    for index, gameweek in enumerate(gameweeks):
        try:
            frame = predict(int(gameweek))
        except Exception as error:  # e.g. no fixtures published yet
            notes.append(f"GW{gameweek} could not be projected ({error}); counted as 0.")
            continue
        frame = frame.drop_duplicates("id")
        ids = pd.to_numeric(frame["id"], errors="coerce").astype(int)
        points = pd.to_numeric(frame["expected_points"], errors="coerce").fillna(0.0)
        if "availability_factor" in frame:
            factor = pd.to_numeric(frame["availability_factor"], errors="coerce").fillna(1.0)
        else:
            factor = pd.Series(1.0, index=frame.index)
        if index > 0:
            doubtful = (factor > 0) & (factor < 1)
            points = points.where(~doubtful, points / factor.where(doubtful, 1.0))
            factor = factor.where(~doubtful, 1.0)
        columns[int(gameweek)] = pd.Series(points.to_numpy(), index=ids.to_numpy())
        factor_columns[int(gameweek)] = pd.Series(factor.to_numpy(dtype=float), index=ids.to_numpy())
        identities.append(
            pd.DataFrame(
                {
                    "id": ids.to_numpy(),
                    "web_name": frame["web_name"].to_numpy(),
                    "team_name": frame["team_name"].to_numpy(),
                    "element_type": frame["element_type"].to_numpy(),
                }
            )
        )
    if not columns:
        raise ValueError("No gameweek in the horizon could be projected")
    points = pd.DataFrame(columns).reindex(columns=list(gameweeks)).fillna(0.0)
    # The availability factor each gameweek's points already include, so a
    # manager's own availability can replace it (see src/my_team.py).
    points.attrs["factors"] = (
        pd.DataFrame(factor_columns).reindex(index=points.index, columns=list(gameweeks)).fillna(1.0)
    )
    identity = pd.concat(identities).drop_duplicates("id").set_index("id")
    if len(gameweeks) > 1:
        notes.append(
            "After the first gameweek, doubtful players count as fit; players ruled "
            "out (injured, suspended, left) stay at zero."
        )
    return points, identity, notes


# ----------------------------------------------------------------------
# The optimiser
# ----------------------------------------------------------------------


def candidate_pool(
    players: pd.DataFrame,
    current_ids: Iterable[int],
    gameweeks: Sequence[int],
    *,
    locked_ids: Iterable[int] = (),
    banned_ids: Iterable[int] = (),
    max_per_club: int = 3,
) -> pd.DataFrame:
    """Drop only candidates that provably cannot change the optimum.

    ``players`` needs ``id``, ``element_type``, ``team_name``, ``cost``,
    ``buyable`` and one points column per gameweek. The current squad and
    locked players are always kept; banned players and players who can't
    be bought (and aren't owned) are dropped, as the solver could never pick
    them.

    A buyable candidate *p* is also dropped when other buyable candidates
    of his position *dominate* him (cost no higher, points no lower in every
    gameweek, ties broken by id so dominance has no cycles) from at least
    ``quota + full_clubs`` distinct clubs, where ``full_clubs`` is the most
    clubs a squad can fill (5). Proof that the optimum survives: in an
    optimal squad holding *p*, at most ``quota - 1`` dominators share his
    position in the squad and at most ``full_clubs`` clubs are full, so a
    dominator outside the squad from a club with room remains. Swapping him
    in keeps every constraint (same position, no higher cost, same transfer
    count, as neither is owned) and loses no points in any gameweek, so the
    squad stays optimal. Each swap moves strictly up the dominance order, so
    repeating it ends in an optimal squad drawn from the kept players.
    """
    current = {int(value) for value in current_ids}
    locked = {int(value) for value in locked_ids}
    banned = {int(value) for value in banned_ids}
    ids = players["id"].astype(int).to_numpy()
    owned = np.isin(ids, list(current | locked))
    allowed = players["buyable"].astype(bool).to_numpy() & ~np.isin(ids, list(banned))
    candidates = allowed & ~owned
    keep = owned | candidates
    positions = players["element_type"].astype(int).to_numpy()
    clubs = players["team_name"].astype(str).to_numpy()
    costs = players["cost"].astype(float).to_numpy()
    points = players[list(gameweeks)].astype(float).to_numpy()
    full_clubs = SQUAD_SIZE // max(1, int(max_per_club))
    for position, quota in SQUAD_QUOTAS.items():
        members = np.flatnonzero(candidates & (positions == position))
        if len(members) <= quota + full_clubs:
            continue
        cost = costs[members]
        pts = points[members]
        no_dearer = cost[None, :] <= cost[:, None] + 1e-9
        no_worse = (pts[None, :, :] >= pts[:, None, :] - 1e-9).all(axis=2)
        same = (np.abs(cost[None, :] - cost[:, None]) <= 1e-9) & (
            np.abs(pts[None, :, :] - pts[:, None, :]) <= 1e-9
        ).all(axis=2)
        earlier = ids[members][None, :] < ids[members][:, None]
        dominated_by = no_dearer & no_worse & (~same | earlier)
        np.fill_diagonal(dominated_by, False)
        member_clubs = clubs[members]
        for row, index in enumerate(members):
            if len(set(member_clubs[dominated_by[row]])) >= quota + full_clubs:
                keep[index] = False
    return players.loc[keep].reset_index(drop=True)


def solve_squad(
    players: pd.DataFrame,
    gameweeks: Sequence[int],
    *,
    budget: float,
    current_ids: Iterable[int] = (),
    transfers_exact: Optional[int] = None,
    free_transfers: Optional[int] = None,
    bench_weight: float = 0.1,
    hit_cost: float = HIT_COST,
    max_per_club: int = 3,
    time_limit: float = 20.0,
    locked_ids: Iterable[int] = (),
    banned_ids: Iterable[int] = (),
) -> Optional[Dict[str, Any]]:
    """Find the squad with the highest projected points over ``gameweeks``.

    ``players`` needs ``id``, ``element_type`` (1–4), ``team_name``,
    ``cost`` (selling price for current players, price otherwise),
    ``buyable`` and one points column per gameweek. With
    ``transfers_exact`` the squad must differ from ``current_ids`` by
    exactly that many players, and transfers beyond ``free_transfers`` cost
    ``hit_cost`` each. Locked players must be in the squad (bought if
    need be) and banned ones must not (sold if owned). Returns None when no
    legal squad exists.
    """
    gameweeks = [int(gameweek) for gameweek in gameweeks]
    n = len(players)
    horizon = len(gameweeks)
    current = set(int(value) for value in current_ids)
    ids = players["id"].astype(int).to_numpy()
    positions = players["element_type"].astype(int).to_numpy()
    clubs = players["team_name"].astype(str).to_numpy()
    costs = players["cost"].astype(float).to_numpy()
    points = players[gameweeks].astype(float).to_numpy()  # n x horizon
    in_current = np.isin(ids, list(current))
    transfers_mode = transfers_exact is not None

    def x(i: int) -> int:
        return i

    def s(i: int, g: int) -> int:
        return n + g * n + i

    def c(i: int, g: int) -> int:
        return n + horizon * n + g * n + i

    hits_var = n + 2 * horizon * n
    size = hits_var + (1 if transfers_mode else 0)

    objective = np.zeros(size)
    for g in range(horizon):
        for i in range(n):
            objective[x(i)] -= bench_weight * points[i, g]
            objective[s(i, g)] -= (1.0 - bench_weight) * points[i, g]
            objective[c(i, g)] -= points[i, g]
    if transfers_mode:
        objective[hits_var] = hit_cost

    rows: List[int] = []
    cols: List[int] = []
    values: List[float] = []
    lower: List[float] = []
    upper: List[float] = []

    def add(terms: Iterable[Tuple[int, float]], lo: float, hi: float) -> None:
        row = len(lower)
        for col, value in terms:
            rows.append(row)
            cols.append(col)
            values.append(value)
        lower.append(lo)
        upper.append(hi)

    add(((x(i), 1.0) for i in range(n)), SQUAD_SIZE, SQUAD_SIZE)
    for position, quota in SQUAD_QUOTAS.items():
        add(((x(i), 1.0) for i in range(n) if positions[i] == position), quota, quota)
    for club in sorted(set(clubs)):
        members = [i for i in range(n) if clubs[i] == club]
        limit = max_per_club
        if transfers_exact == 0:
            # A real-life move can leave a squad over the limit; keeping it
            # as it is stays legal.
            limit = max(limit, int(sum(in_current[i] for i in members)))
        add(((x(i), 1.0) for i in members), 0, limit)
    add(((x(i), costs[i]) for i in range(n)), -np.inf, budget + 1e-6)
    for g in range(horizon):
        add(((s(i, g), 1.0) for i in range(n)), STARTERS, STARTERS)
        add(((c(i, g), 1.0) for i in range(n)), 1, 1)
        for position in SQUAD_QUOTAS:
            add(
                ((s(i, g), 1.0) for i in range(n) if positions[i] == position),
                STARTER_MIN[position],
                STARTER_MAX[position],
            )
        for i in range(n):
            add(((s(i, g), 1.0), (x(i), -1.0)), -np.inf, 0)
            add(((c(i, g), 1.0), (s(i, g), -1.0)), -np.inf, 0)
    if transfers_mode:
        incoming = [(x(i), 1.0) for i in range(n) if not in_current[i]]
        add(incoming, transfers_exact, transfers_exact)
        free = SQUAD_SIZE if free_transfers is None else int(free_transfers)
        # hits >= transfers - free
        add([(hits_var, 1.0)] + [(col, -1.0) for col, _ in incoming], -free, np.inf)

    matrix = coo_matrix((values, (rows, cols)), shape=(len(lower), size)).tocsr()
    lower_bounds = np.zeros(size)
    upper_bounds = np.ones(size)
    buyable = players["buyable"].astype(bool).to_numpy()
    locked = set(int(value) for value in locked_ids)
    banned = set(int(value) for value in banned_ids)
    for i in range(n):
        if not in_current[i] and not buyable[i]:
            upper_bounds[x(i)] = 0
        if int(ids[i]) in banned:
            upper_bounds[x(i)] = 0
        if int(ids[i]) in locked:
            if upper_bounds[x(i)] == 0:
                return None
            lower_bounds[x(i)] = 1
    if not locked <= set(int(value) for value in ids):
        return None
    if transfers_mode:
        upper_bounds[hits_var] = SQUAD_SIZE
    result = milp(
        objective,
        constraints=LinearConstraint(matrix, lower, upper),
        integrality=np.ones(size),
        bounds=Bounds(lower_bounds, upper_bounds),
        options={"time_limit": time_limit},
    )
    if result.x is None:
        return None
    solution = np.round(result.x).astype(int)
    chosen = [i for i in range(n) if solution[x(i)] == 1]
    lineups = []
    for g, gameweek in enumerate(gameweeks):
        starters = [i for i in chosen if solution[s(i, g)] == 1]
        captain = next(i for i in chosen if solution[c(i, g)] == 1)
        vice = max(
            (i for i in starters if i != captain), key=lambda i: points[i, g], default=None
        )
        total = float(points[starters, g].sum() + points[captain, g])
        lineups.append(
            {
                "gameweek": gameweek,
                "starting_ids": [int(ids[i]) for i in starters],
                "captain_id": int(ids[captain]),
                "vice_id": int(ids[vice]) if vice is not None else None,
                "points": round(total, 2),
                "bench_points": round(float(points[[i for i in chosen if i not in starters], g].sum()), 2),
            }
        )
    transfers_made = int(sum(1 for i in chosen if not in_current[i]))
    hits = int(solution[hits_var]) if transfers_mode else 0
    spent = float(costs[chosen].sum())
    playing = sum(item["points"] for item in lineups)
    bench = sum(item["bench_points"] for item in lineups)
    return {
        "squad_ids": [int(ids[i]) for i in chosen],
        "lineups": lineups,
        "projected_total": round(playing, 2),
        # What the solver maximises: each gameweek's XI and captain, a
        # fraction of the bench, minus hits. Plans are ranked by it.
        "objective": round(playing + bench_weight * bench - hits * hit_cost, 3),
        "transfers": transfers_made,
        "hits": hits,
        "hit_cost": round(hits * hit_cost, 1),
        "spent": round(spent, 1),
        "bank_after": round(budget - spent, 1),
        "optimal": bool(result.status == 0),
    }


def _player_summary(row: pd.Series, gameweeks: Sequence[int]) -> Dict[str, Any]:
    position = int(row["element_type"])
    per_gameweek = [round(float(row[gameweek]), 2) for gameweek in gameweeks]
    return {
        "id": int(row["id"]),
        "web_name": row.get("web_name"),
        "team_name": row.get("team_name"),
        "element_type": POSITION_NAMES[position],
        "position": POSITION_SHORT[position],
        "price": None if pd.isna(row.get("price")) else round(float(row["price"]), 1),
        "cost": round(float(row["cost"]), 1),
        "points": per_gameweek,
        "total": round(sum(per_gameweek), 2),
        "availability_factor": None
        if pd.isna(row.get("availability_factor"))
        else round(float(row["availability_factor"]), 3),
        "news": row.get("news") or "",
    }


def describe_plan(
    solution: Dict[str, Any],
    players: pd.DataFrame,
    gameweeks: Sequence[int],
    current_ids: Iterable[int],
) -> Dict[str, Any]:
    """Turn a solution into squad records and paired moves for the dashboard."""
    by_id = players.set_index("id", drop=False)
    current = [int(value) for value in current_ids]
    squad = set(solution["squad_ids"])
    first = solution["lineups"][0]
    starting = set(first["starting_ids"])
    records = []
    for player_id in solution["squad_ids"]:
        record = _player_summary(by_id.loc[player_id], gameweeks)
        record.update(
            new=player_id not in current,
            starting=player_id in starting,
            role="captain"
            if player_id == first["captain_id"]
            else "vice"
            if player_id == first["vice_id"]
            else "",
        )
        records.append(record)
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    records.sort(key=lambda item: (not item["starting"], order[item["position"]], -item["total"]))
    outgoing = [
        _player_summary(by_id.loc[player_id], gameweeks)
        for player_id in current
        if player_id not in squad and player_id in by_id.index
    ]
    incoming = [record for record in records if record["new"]]
    moves = []
    for position in ("GK", "DEF", "MID", "FWD"):
        outs = sorted((item for item in outgoing if item["position"] == position), key=lambda item: item["total"])
        ins = sorted((item for item in incoming if item["position"] == position), key=lambda item: -item["total"])
        for out_player, in_player in zip(outs, ins):
            moves.append(
                {
                    "out": out_player,
                    "in": in_player,
                    "gain": round(in_player["total"] - out_player["total"], 2),
                    "cost": round(in_player["cost"] - out_player["cost"], 1),
                }
            )
    return {**solution, "squad": records, "moves": moves}


def plan(
    players: pd.DataFrame,
    gameweeks: Sequence[int],
    *,
    mode: str,
    current_ids: Iterable[int],
    bank: float,
    free_transfers: Optional[int] = None,
    max_transfers: Optional[int] = None,
    bench_weight: float = 0.1,
    locked_ids: Iterable[int] = (),
    banned_ids: Iterable[int] = (),
) -> Dict[str, Any]:
    """Plans for a mode, each compared with keeping the current squad.

    ``players`` carries ``cost`` (selling price for current players), and
    ``budget`` is the bank plus the current squad's selling value.
    Transfers mode returns one plan per number of transfers, from rolling
    (0) to ``max_transfers``; the best net plan is marked. Locks and bans
    bind every plan except the roll, which keeps the squad as it is.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode!r}")
    locked = sorted(set(int(value) for value in locked_ids))
    banned = sorted(set(int(value) for value in banned_ids))
    both = set(locked) & set(banned)
    if both:
        raise ValueError(f"Players cannot be both locked and banned: {sorted(both)}")
    current = [int(value) for value in current_ids]
    in_pool = players["id"].isin(current)
    if int(in_pool.sum()) != SQUAD_SIZE:
        raise ValueError("The current squad must have 15 players with projections or prices")
    budget = float(bank) + float(players.loc[in_pool, "cost"].sum())
    players = players.copy()
    players["total"] = players[list(gameweeks)].sum(axis=1)
    pool = candidate_pool(players, current, gameweeks, locked_ids=locked, banned_ids=banned)
    constraints = {"locked_ids": locked, "banned_ids": banned}
    blocked = "No legal squad within the budget" + (
        " and your locks and bans" if locked or banned else ""
    )

    baseline = solve_squad(
        pool, gameweeks, budget=budget, current_ids=current, transfers_exact=0,
        free_transfers=free_transfers, bench_weight=bench_weight,
    )
    if baseline is None:
        raise ValueError("The current squad could not be evaluated")
    baseline_total = baseline["projected_total"]
    baseline_objective = baseline["objective"]

    def finish(solution: Dict[str, Any]) -> Dict[str, Any]:
        described = describe_plan(solution, pool, gameweeks, current)
        net = solution["projected_total"] - solution["hit_cost"]
        described.update(
            net_total=round(net, 2),
            gross_gain=round(solution["projected_total"] - baseline_total, 2),
            net_gain=round(net - baseline_total, 2),
            objective_gain=round(solution["objective"] - baseline_objective, 3),
        )
        return described

    plans = []
    if mode == "transfers":
        free = SQUAD_SIZE if free_transfers is None else int(free_transfers)
        limit = max_transfers if max_transfers is not None else min(SQUAD_SIZE, free + 2)
        for count in range(0, int(limit) + 1):
            solution = baseline if count == 0 else solve_squad(
                pool, gameweeks, budget=budget, current_ids=current,
                transfers_exact=count, free_transfers=free, bench_weight=bench_weight,
                **constraints,
            )
            if solution is None:
                plans.append({"transfers": count, "feasible": False, "reason": blocked})
                continue
            plans.append({"feasible": True, **finish(solution)})
    else:
        solution = solve_squad(
            pool, gameweeks, budget=budget, current_ids=current, bench_weight=bench_weight,
            **constraints,
        )
        if solution is None:
            raise ValueError(blocked)
        plans.append({"feasible": True, **finish(solution)})

    feasible = [index for index, item in enumerate(plans) if item.get("feasible")]
    # Ranked by the solver's own objective, so the plan marked best is the
    # one the optimisation itself prefers (ties keep fewer transfers).
    best = max(
        feasible,
        key=lambda index: (round(plans[index]["objective"], 6), -plans[index]["transfers"]),
    )
    return {
        "mode": mode,
        "gameweeks": list(gameweeks),
        "budget": round(budget, 1),
        "bank": round(float(bank), 1),
        "free_transfers": free_transfers,
        "baseline": finish(baseline),
        "plans": plans,
        "best_index": best,
        "pool_size": int(len(pool)),
        "candidates": int(len(players)),
        "bench_weight": bench_weight,
        "locked_ids": locked,
        "banned_ids": banned,
    }

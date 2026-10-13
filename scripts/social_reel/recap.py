"""Score a locked scout squad against what actually happened.

Standard library only, like ``data.py``. Points come from the official FPL
``/event/{gw}/live/`` response; the GW average and finished flags come from
``/bootstrap-static/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .data import SITE_URL, Player, Squad

POSITION_ORDER = ("GK", "DEF", "MID", "FWD")


class RecapError(ValueError):
    """The squad cannot be scored honestly (not locked, or points missing)."""


def best_xi(players: Sequence[Player]) -> Tuple[List[Player], Tuple[int, int, int]]:
    """Highest-xP legal XI, the same rule as ``src/team_rating._best_benchmark_lineup``."""
    grouped = {pos: sorted((p for p in players if p.position == pos), key=lambda p: -p.xp)
               for pos in POSITION_ORDER}
    best: List[Player] = []
    best_total, best_shape = -1.0, (0, 0, 0)
    for defenders in range(3, 6):
        for midfielders in range(2, 6):
            forwards = 10 - defenders - midfielders
            if forwards < 1 or forwards > 3:
                continue
            counts = {"GK": 1, "DEF": defenders, "MID": midfielders, "FWD": forwards}
            if any(len(grouped[pos]) < n for pos, n in counts.items()):
                continue
            lineup = [p for pos, n in counts.items() for p in grouped[pos][:n]]
            total = sum(p.xp for p in lineup)
            if total > best_total:
                best, best_total, best_shape = lineup, total, (defenders, midfielders, forwards)
    if not best:
        raise RecapError("Could not pick a legal starting XI from the squad.")
    return best, best_shape


def points_from_live(live: Mapping[str, Any]) -> Dict[int, Dict[str, int]]:
    """``{element_id: stats}`` from an official ``/event/{gw}/live/`` payload."""
    out: Dict[int, Dict[str, int]] = {}
    for element in live.get("elements") or []:
        stats = element.get("stats") or {}
        out[int(element["id"])] = {k: int(v) for k, v in stats.items() if isinstance(v, (int, float))}
    return out


@dataclass(frozen=True)
class GameweekInfo:
    average: Optional[int] = None
    highest: Optional[int] = None
    finished: bool = False
    data_checked: bool = False


def gameweek_info(bootstrap: Mapping[str, Any], gameweek: int) -> GameweekInfo:
    for event in bootstrap.get("events") or []:
        if int(event.get("id", 0)) == gameweek:
            return GameweekInfo(
                average=event.get("average_entry_score"),
                highest=event.get("highest_score"),
                finished=bool(event.get("finished")),
                data_checked=bool(event.get("data_checked")),
            )
    return GameweekInfo()


@dataclass(frozen=True)
class Recap:
    squad: Squad
    points: Mapping[str, int]  # player name -> actual FPL points
    xi: Tuple[Player, ...]
    shape: Tuple[int, int, int]
    info: GameweekInfo

    @property
    def formation(self) -> str:
        return "-".join(str(n) for n in self.shape)

    @property
    def xi_names(self) -> frozenset:
        return frozenset(p.name for p in self.xi)

    def pts(self, player: Player) -> int:
        return self.points[player.name]

    @property
    def total(self) -> int:
        """XI points plus the captain's points again (armband doubles)."""
        return sum(self.pts(p) for p in self.xi) + self.pts(self.squad.captain)

    @property
    def xp_total(self) -> float:
        return sum(p.xp for p in self.xi) + self.squad.captain.xp

    def hauls(self, n: int = 5) -> List[Player]:
        return sorted(self.squad.players, key=lambda p: (-self.pts(p), -p.xp))[:n]

    def misses(self, n: int = 2) -> List[Player]:
        """Starters furthest below their forecast."""
        return sorted(self.xi, key=lambda p: (self.pts(p) - p.xp, p.name))[:n]

    def beat_count(self) -> int:
        return sum(self.pts(p) >= p.xp for p in self.squad.players)

    def hindsight_captain(self) -> Player:
        return max(self.squad.players, key=lambda p: (self.pts(p), p.xp))


def build_recap(squad: Squad, points: Mapping[int, Mapping[str, int]], info: GameweekInfo) -> Recap:
    if not squad.frozen:
        raise RecapError(f"GW{squad.gameweek} forecast is not locked; a recap would compare against a moving target.")
    missing = [p.name for p in squad.players if p.id is None or p.id not in points]
    if missing:
        raise RecapError("No live points for: " + ", ".join(missing) + ".")
    by_name = {p.name: int(points[p.id].get("total_points", 0)) for p in squad.players}
    xi, shape = best_xi(squad.players)
    if squad.captain not in xi:
        raise RecapError(f"Captain {squad.captain.name} is not in the best-xP XI.")
    return Recap(squad=squad, points=by_name, xi=tuple(xi), shape=shape, info=info)


def _pts(n: int) -> str:
    return f"{n} pt" if n == 1 else f"{n} pts"


def recap_caption(r: Recap) -> str:
    sq = r.squad
    c, v = sq.captain, sq.vice
    head = f"The locked forecast scored {r.total} pts"
    if r.info.average is not None:
        head += f" ({r.total - r.info.average:+d} on the {r.info.average}-pt GW average)"
    lines = [
        f"OpenFPL Scout AI — Gameweek {sq.gameweek} recap 🦁",
        "",
        head + ".",
        f"Best-xP starting XI ({r.formation}) + captain; forecast was {r.xp_total:.1f} xP.",
        "",
        f"©️ {c.name}: {_pts(r.pts(c))} ×2 = {r.pts(c) * 2} (forecast {c.xp:.1f} xP)",
        f"Ⓥ {v.name}: {_pts(r.pts(v))} (forecast {v.xp:.1f} xP)",
        "",
        "Top hauls:",
    ]
    lines += [f"{i}. {p.name} — {_pts(r.pts(p))} (xP {p.xp:.1f})" for i, p in enumerate(r.hauls(5), 1)]
    lines += ["", "Misses: " + " · ".join(f"{p.name} {_pts(r.pts(p))} (xP {p.xp:.1f})" for p in r.misses(2))]
    lines += [
        "",
        "Forecast locked at the deadline — no hindsight edits.",
        f"GW{sq.gameweek + 1} squad is live 👉 {SITE_URL}",
        "",
        f"#FPL #FantasyPremierLeague #FPLCommunity #FPLTips #GW{sq.gameweek}",
    ]
    return "\n".join(lines) + "\n"

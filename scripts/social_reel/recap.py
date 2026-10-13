"""Score a locked scout squad against what actually happened.

Standard library only, like ``data.py``. Points come from the official FPL
``/event/{gw}/live/`` response; the GW average and finished flags come from
``/bootstrap-static/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    deadline: Optional[str] = None
    next_gameweek: Optional[int] = None  # the event FPL marks is_next


def gameweek_info(bootstrap: Mapping[str, Any], gameweek: int) -> GameweekInfo:
    events = bootstrap.get("events") or []
    upcoming = next((int(e["id"]) for e in events if e.get("is_next")), None)
    for event in events:
        if int(event.get("id", 0)) == gameweek:
            return GameweekInfo(
                average=event.get("average_entry_score"),
                highest=event.get("highest_score"),
                finished=bool(event.get("finished")),
                data_checked=bool(event.get("data_checked")),
                deadline=event.get("deadline_time"),
                next_gameweek=upcoming,
            )
    return GameweekInfo()


def _legal(lineup: Sequence[Player]) -> bool:
    count = {pos: sum(p.position == pos for p in lineup) for pos in POSITION_ORDER}
    return count["GK"] == 1 and count["DEF"] >= 3 and count["MID"] >= 2 and count["FWD"] >= 1


def auto_subs(xi: Sequence[Player], bench: Sequence[Player], minutes: Mapping[str, int]
              ) -> Tuple[List[Player], List[Tuple[Player, Player]]]:
    """FPL automatic substitutions: a starter with 0 minutes is replaced by the first bench
    player (keeper for keeper; outfield in bench order) who played and keeps the formation legal."""
    lineup, subs, used = list(xi), [], set()
    for starter in xi:
        if minutes.get(starter.name, 0) > 0:
            continue
        for sub in bench:
            if sub.name in used or minutes.get(sub.name, 0) <= 0:
                continue
            if (starter.position == "GK") != (sub.position == "GK"):
                continue
            trial = [sub if p is starter else p for p in lineup]
            if _legal(trial):
                lineup, used = trial, used | {sub.name}
                subs.append((starter, sub))
                break
    return lineup, subs


@dataclass(frozen=True)
class Recap:
    squad: Squad
    points: Mapping[str, int]  # player name -> actual FPL points
    picked: Tuple[Player, ...]  # best-xP XI as selected before the games
    shape: Tuple[int, int, int]  # formation of the picked XI
    info: GameweekInfo
    minutes: Mapping[str, int] = None  # player name -> minutes played
    xi: Tuple[Player, ...] = ()  # XI that scored, after automatic substitutions
    subs: Tuple[Tuple[Player, Player], ...] = ()  # (off, on)
    armband: Optional[Player] = None  # captain, or the vice if the captain did not play

    @property
    def locked_pre_deadline(self) -> bool:
        """True only when the forecast was captured before the deadline (no hindsight)."""
        if self.squad.freeze_method != "deadline":
            return False
        captured, deadline = _when(self.squad.captured_at), _when(self.info.deadline or self.squad.deadline)
        return captured is None or deadline is None or captured < deadline

    @property
    def forecast_word(self) -> str:
        return "locked forecast" if self.locked_pre_deadline else "retro forecast"

    @property
    def captured_label(self) -> Optional[str]:
        captured = _when(self.squad.captured_at)
        return f"{captured.day} {captured:%b %Y}" if captured else None

    @property
    def cta_gameweek(self) -> int:
        return self.info.next_gameweek or self.squad.gameweek + 1

    @property
    def formation(self) -> str:
        return "-".join(str(n) for n in self.shape)

    @property
    def xi_names(self) -> frozenset:
        return frozenset(p.name for p in self.xi)

    @property
    def subbed_off(self) -> frozenset:
        return frozenset(off.name for off, _ in self.subs)

    @property
    def subbed_on(self) -> frozenset:
        return frozenset(on.name for _, on in self.subs)

    def pts(self, player: Player) -> int:
        return self.points[player.name]

    @property
    def armband_player(self) -> Player:
        return self.armband or self.squad.captain

    @property
    def total(self) -> int:
        """Scoring XI points plus the armband's points again (doubled)."""
        return sum(self.pts(p) for p in self.xi) + self.pts(self.armband_player)

    @property
    def xp_total(self) -> float:
        """Forecast for the picked XI with the captain doubled."""
        return sum(p.xp for p in self.picked) + self.squad.captain.xp

    def hauls(self, n: int = 5) -> List[Player]:
        return sorted(self.squad.players, key=lambda p: (-self.pts(p), -p.xp))[:n]

    def misses(self, n: int = 2) -> List[Player]:
        """Picked starters furthest below their forecast."""
        return sorted(self.picked, key=lambda p: (self.pts(p) - p.xp, p.name))[:n]

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
    minutes = {p.name: int(points[p.id].get("minutes", 0)) for p in squad.players}
    picked, shape = best_xi(squad.players)
    if squad.captain not in picked:
        raise RecapError(f"Captain {squad.captain.name} is not in the best-xP XI.")
    names = {p.name for p in picked}
    rest = [p for p in squad.players if p.name not in names]
    bench = sorted((p for p in rest if p.position == "GK"), key=lambda p: -p.xp) + \
        sorted((p for p in rest if p.position != "GK"), key=lambda p: -p.xp)
    xi, subs = auto_subs(picked, bench, minutes)
    armband = squad.captain
    if minutes[squad.captain.name] <= 0 and minutes[squad.vice.name] > 0 and squad.vice in xi:
        armband = squad.vice
    return Recap(squad=squad, points=by_name, picked=tuple(picked), shape=shape, info=info, minutes=minutes,
                 xi=tuple(xi), subs=tuple(subs), armband=armband)


def _when(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _pts(n: int) -> str:
    return f"{n} pt" if n == 1 else f"{n} pts"


def recap_caption(r: Recap) -> str:
    sq = r.squad
    c, v = sq.captain, sq.vice
    head = f"The {r.forecast_word} scored {r.total} pts"
    if r.info.average is not None:
        head += f" ({r.total - r.info.average:+d} on the {r.info.average}-pt GW average)"
    lines = [
        f"OpenFPL Scout AI — Gameweek {sq.gameweek} recap 🦁",
        "",
        head + ".",
        f"Best-xP starting XI ({r.formation}) + captain; the {r.forecast_word} was {r.xp_total:.1f} xP.",
    ]
    lines += [f"Auto-sub: {on.name} ({_pts(r.pts(on))}) on for {off.name} (didn't play)." for off, on in r.subs]
    arm = r.armband_player
    lines += [
        "",
        f"©️ {c.name}: {_pts(r.pts(c))}" + (f" ×2 = {r.pts(c) * 2}" if arm is c else " (didn't play)")
        + f" (forecast {c.xp:.1f} xP)",
        f"Ⓥ {v.name}: {_pts(r.pts(v))}" + (f" ×2 = {r.pts(v) * 2}, took the armband" if arm is v else "")
        + f" (forecast {v.xp:.1f} xP)",
        "",
        "Top hauls:",
    ]
    lines += [f"{i}. {p.name} — {_pts(r.pts(p))} (xP {p.xp:.1f})" for i, p in enumerate(r.hauls(5), 1)]
    lines += ["", "Misses: " + " · ".join(f"{p.name} {_pts(r.pts(p))} (xP {p.xp:.1f})" for p in r.misses(2))]
    lines += [
        "",
        ("Forecast locked at the deadline — no hindsight edits." if r.locked_pre_deadline else
         "Note: retro forecast — made " + (f"on {r.captured_label}, " if r.captured_label else "")
         + "after this gameweek's deadline, so it is not a live pick. "
         "Live forecasts now lock at the deadline."),
        f"GW{r.cta_gameweek} squad is live 👉 {SITE_URL}",
        "",
        f"#FPL #FantasyPremierLeague #FPLCommunity #FPLTips #GW{sq.gameweek}",
    ]
    return "\n".join(lines) + "\n"

"""Score a locked scout squad against what actually happened.

Standard library only, like ``data.py``. Points come from the official FPL
``/event/{gw}/live/`` response; the GW average and finished flags come from
``/bootstrap-static/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .data import DIFF_MAX_OWNED, SITE_URL, Differential, Player, Squad

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
class DiffResult:
    """A differential from the preview card, with what it actually scored."""
    diff: Differential
    points: int
    minutes: int

    @property
    def player(self) -> Player:
        return self.diff.player


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
    diffs: Tuple[DiffResult, ...] = ()  # the preview's differentials, scored
    diff_source: str = ""  # "preview" (saved by the preview build) or "retro" (rebuilt afterwards)
    diff_captured_at: Optional[str] = None

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

    @property
    def diffs_locked(self) -> bool:
        """True only when the picks were saved by a preview build before the deadline."""
        if self.diff_source != "preview":
            return False
        captured, deadline = _when(self.diff_captured_at), _when(self.info.deadline or self.squad.deadline)
        return captured is not None and deadline is not None and captured < deadline

    @property
    def diff_total(self) -> int:
        return sum(r.points for r in self.diffs)

    @property
    def diff_xp(self) -> float:
        return sum(r.player.xp for r in self.diffs)

    def diff_best(self, n: int = 2) -> List[DiffResult]:
        return sorted(self.diffs, key=lambda r: (-r.points, -r.player.xp))[:n]

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


def attach_differentials(recap: Recap, picks: Sequence[Differential], points: Mapping[int, Mapping[str, int]],
                          source: str, captured_at: Optional[str] = None) -> Recap:
    """Score the preview's differentials; refuses (RecapError) if any has no live points."""
    from dataclasses import replace

    missing = [d.player.name for d in picks if d.player.id is None or d.player.id not in points]
    if missing:
        raise RecapError("No live points for differentials: " + ", ".join(missing) + ".")
    if any(d.player.id in {p.id for p in recap.squad.players} for d in picks):
        raise RecapError("A differential is in the model's 15; the file is not for this squad.")
    results = tuple(DiffResult(d, int(points[d.player.id].get("total_points", 0)),
                               int(points[d.player.id].get("minutes", 0))) for d in picks)
    return replace(recap, diffs=results, diff_source=source, diff_captured_at=captured_at)


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
    if r.diffs:
        when = "picked before the deadline" if r.diffs_locked else "retro pick, ownership rebuilt after the deadline"
        lines += ["", f"Differentials (under {DIFF_MAX_OWNED:g}% owned, outside the 15; {when}):",
                  f"{len(r.diffs)} picks scored {_pts(r.diff_total)} vs {r.diff_xp:.1f} xP. Best: "
                  + ", ".join(f"{x.player.name} {_pts(x.points)}" for x in r.diff_best(2)) + "."]
        for pos in POSITION_ORDER:
            row = [x for x in r.diffs if x.player.position == pos]
            if row:
                lines.append(f"{pos}: " + " · ".join(
                    f"{x.player.name} {_pts(x.points)} (xP {x.player.xp:.1f}, {x.diff.owned:.1f}%)" for x in row))
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

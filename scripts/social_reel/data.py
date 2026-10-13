"""Shape a ``/api/scout`` response into what the reel shows.

Standard library only, so it can be imported and tested without the
rendering dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

POSITIONS = {
    "Goalkeeper": "GK",
    "Defender": "DEF",
    "Midfielder": "MID",
    "Forward": "FWD",
}
POSITION_WORDS = {"GK": "GOALKEEPER", "DEF": "DEFENDER", "MID": "MIDFIELDER", "FWD": "FORWARD"}
SQUAD_SHAPE = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
MAX_PER_CLUB = 3

# Official three-letter codes. Clubs not listed fall back to the first three
# letters of their name, so a promoted side never breaks a render.
CLUB_CODES = {
    "Arsenal": "ARS", "Aston Villa": "AVL", "Bournemouth": "BOU", "Brentford": "BRE",
    "Brighton": "BHA", "Burnley": "BUR", "Chelsea": "CHE", "Coventry City": "COV",
    "Crystal Palace": "CRY", "Everton": "EVE", "Fulham": "FUL", "Hull City": "HUL",
    "Ipswich": "IPS", "Ipswich Town": "IPS", "Leeds": "LEE", "Leicester": "LEI",
    "Leicester City": "LEI", "Liverpool": "LIV", "Luton": "LUT", "Manchester City": "MCI",
    "Manchester United": "MUN", "Middlesbrough": "MID", "Millwall": "MIL",
    "Newcastle United": "NEW", "Newcastle": "NEW", "Norwich": "NOR", "Norwich City": "NOR",
    "Nottingham Forest": "NFO", "Sheffield United": "SHU", "Sheffield Utd": "SHU",
    "Southampton": "SOU", "Stoke City": "STK", "Sunderland": "SUN", "Swansea City": "SWA",
    "Tottenham": "TOT", "Tottenham Hotspur": "TOT", "Watford": "WAT", "West Bromwich Albion": "WBA",
    "West Brom": "WBA", "West Ham": "WHU", "West Ham United": "WHU", "Wolves": "WOL",
    "Wolverhampton Wanderers": "WOL", "Bristol City": "BRC", "Derby County": "DER",
    "Preston North End": "PNE", "Queens Park Rangers": "QPR", "Charlton Athletic": "CHA",
    "Wrexham": "WRX", "Birmingham City": "BIR", "Portsmouth": "POR", "Blackburn Rovers": "BLA",
}
CLUB_SHORT = {
    "Manchester City": "Man City", "Manchester United": "Man Utd", "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nott'm Forest", "Tottenham Hotspur": "Tottenham",
    "Wolverhampton Wanderers": "Wolves", "West Ham United": "West Ham",
    "West Bromwich Albion": "West Brom", "Sheffield United": "Sheffield Utd",
}

SITE_URL = "openfpl.kassem.dev"


class SquadError(ValueError):
    """The scout payload cannot be rendered as a full 15-man squad."""


def club_code(name: str) -> str:
    name = (name or "").strip()
    if name in CLUB_CODES:
        return CLUB_CODES[name]
    letters = re.sub(r"[^A-Za-z]", "", name)
    return (letters[:3] or "???").upper()


def club_short(name: str) -> str:
    return CLUB_SHORT.get(name, name)


@dataclass(frozen=True)
class Player:
    name: str
    position: str  # GK / DEF / MID / FWD
    club: str
    opponents: Tuple[str, ...]
    home: bool
    fixture_count: int
    xp: float
    role: str = ""  # "C", "V" or ""
    id: Optional[int] = None  # FPL element id

    @property
    def club_code(self) -> str:
        return club_code(self.club)

    @property
    def club_short(self) -> str:
        return club_short(self.club)

    @property
    def venue(self) -> str:
        """Single-letter venue chip: H, A, ×2 for a double, – for a blank."""
        if self.fixture_count > 1:
            return "×2"
        if self.fixture_count == 0:
            return "–"
        return "H" if self.home else "A"

    @property
    def opponent_codes(self) -> str:
        if not self.opponents:
            return "BLANK"
        return "+".join(club_code(o) for o in self.opponents)

    @property
    def fixture(self) -> str:
        """``SUN (A)`` style label."""
        if self.fixture_count == 0 or not self.opponents:
            return "Blank GW"
        if self.fixture_count > 1:
            return f"{self.opponent_codes} (DGW)"
        return f"{self.opponent_codes} ({self.venue})"


@dataclass(frozen=True)
class Squad:
    gameweek: int
    players: Tuple[Player, ...]
    frozen: bool
    captured_at: Optional[str] = None
    version: Optional[str] = None
    deadline: Optional[str] = None  # ISO 8601, from /api/fpl/gameweeks
    stats: Mapping[str, Any] = field(default_factory=dict)
    freeze_method: Optional[str] = None  # "deadline" or "after-deadline"

    @property
    def captain(self) -> Player:
        return next(p for p in self.players if p.role == "C")

    @property
    def vice(self) -> Player:
        return next(p for p in self.players if p.role == "V")

    def by_position(self, position: str) -> List[Player]:
        return sorted((p for p in self.players if p.position == position), key=lambda p: -p.xp)

    def top(self, n: int = 5) -> List[Player]:
        return sorted(self.players, key=lambda p: -p.xp)[:n]


def _opponents(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(o).strip() for o in raw if str(o).strip())
    parts = re.split(r"\s*(?:,|&|/|\+|;)\s*", str(raw))
    return tuple(p for p in parts if p)


def parse_squad(payload: Mapping[str, Any], deadline: Optional[str] = None) -> Squad:
    """Validate a ``/api/scout`` payload and return the squad to render."""
    rows = payload.get("scout_team")
    if not isinstance(rows, list) or not rows:
        raise SquadError("Payload has no scout_team list.")
    gameweek = payload.get("gameweek") or rows[0].get("gameweek")
    try:
        gameweek = int(gameweek)
    except (TypeError, ValueError) as error:
        raise SquadError(f"Gameweek is missing or not a number: {gameweek!r}") from error
    if not 1 <= gameweek <= 38:
        raise SquadError(f"Gameweek {gameweek} is outside 1-38.")

    players = []
    for row in rows:
        position = POSITIONS.get(row.get("element_type"))
        if position is None:
            raise SquadError(f"Unknown position {row.get('element_type')!r} for {row.get('web_name')!r}.")
        role = {"captain": "C", "vice": "V"}.get(str(row.get("role") or "").lower(), "")
        opponents = _opponents(row.get("opponent_team_name"))
        count = row.get("fixture_count")
        count = int(count) if count is not None else len(opponents) or 1
        players.append(Player(
            name=str(row["web_name"]).strip(),
            position=position,
            club=str(row["team_name"]).strip(),
            opponents=opponents,
            home=bool(row.get("was_home")),
            fixture_count=count,
            xp=float(row["expected_points"]),
            role=role,
            id=int(row["id"]) if row.get("id") is not None else None,
        ))

    problems = []
    if len(players) != 15:
        problems.append(f"expected 15 players, got {len(players)}")
    for position, need in SQUAD_SHAPE.items():
        have = sum(p.position == position for p in players)
        if have != need:
            problems.append(f"expected {need} {position}, got {have}")
    for role, label in (("C", "captain"), ("V", "vice-captain")):
        have = sum(p.role == role for p in players)
        if have != 1:
            problems.append(f"expected 1 {label}, got {have}")
    for club, count in club_counts(players).items():
        if count > MAX_PER_CLUB:
            problems.append(f"{count} players from {club}")
    if problems:
        raise SquadError("Scout squad failed validation: " + "; ".join(problems) + ".")

    return Squad(
        gameweek=gameweek,
        players=tuple(players),
        frozen=bool(payload.get("frozen")),
        captured_at=payload.get("forecast_captured_at"),
        version=payload.get("version"),
        deadline=deadline,
        freeze_method=payload.get("freeze_method"),
    )


def club_counts(players: Sequence[Player]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for p in players:
        counts[p.club] = counts.get(p.club, 0) + 1
    return counts


def stacks(squad: Squad, limit: int = 2) -> List[Tuple[str, List[Player]]]:
    """Clubs the model doubled or tripled up on, biggest and strongest first."""
    groups: Dict[str, List[Player]] = {}
    for p in squad.players:
        groups.setdefault(p.club, []).append(p)
    multi = [(club, sorted(ps, key=lambda p: -p.xp)) for club, ps in groups.items() if len(ps) >= 2]
    multi.sort(key=lambda item: (-len(item[1]), -sum(p.xp for p in item[1])))
    return multi[:limit]


def armband_line(squad: Squad) -> str:
    gap = squad.captain.xp - squad.vice.xp
    if gap < 0.2:
        return f"Separated by just {gap:.2f} xP"
    return f"{gap:.1f} xP clear of the vice-captain"


def deadline_label(deadline: Optional[str]) -> Optional[str]:
    """``Sat 18 Oct · 11:00 UK`` from an ISO timestamp, or None."""
    if not deadline:
        return None
    try:
        moment = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError:
        return None
    uk = moment.astimezone(ZoneInfo("Europe/London"))
    return f"{uk:%a} {uk.day} {uk:%b} · {uk:%H:%M} UK"


def status_line(squad: Squad) -> str:
    if squad.frozen:
        return "Locked at the deadline — no hindsight"
    label = deadline_label(squad.deadline)
    if label:
        return f"Deadline {label} · check team news"
    return "Pre-deadline forecast · check team news"


def caption(squad: Squad) -> str:
    """Ready-to-paste post caption."""
    c, v = squad.captain, squad.vice
    lines = [
        f"OpenFPL Scout AI — Gameweek {squad.gameweek} squad 🦁",
        "",
        f"©️ Captain: {c.name} ({c.club_short}, vs {c.fixture}) — {c.xp:.1f} xP",
        f"Ⓥ Vice: {v.name} ({v.club_short}, vs {v.fixture}) — {v.xp:.1f} xP",
        "",
        "Top projected:",
    ]
    lines += [f"{i}. {p.name} — {p.xp:.1f} xP" for i, p in enumerate(squad.top(5), 1)]
    st = stacks(squad)
    if st:
        lines += ["", "Stacks: " + " · ".join(f"{len(ps)}× {club_short(club)}" for club, ps in st)]
    label = deadline_label(squad.deadline)
    lines += [
        "",
        ("Forecast locked at the deadline." if squad.frozen else
         f"Pre-deadline forecast{f' — deadline {label}' if label else ''}. Check team news before you confirm."),
        "",
        "Built by a 4-model ensemble (Ridge · XGBoost · CatBoost · MLP), validated on a full held-out season.",
        f"Full 15, xP for every player and a free team rating 👉 {SITE_URL}",
        "",
        "#FPL #FantasyPremierLeague #FPLCommunity #FPLTips #GW" + str(squad.gameweek),
    ]
    return "\n".join(lines) + "\n"

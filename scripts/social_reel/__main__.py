"""Build the OpenFPL weekly social reel and stills.

Fetch live (next gameweek by default, or a given one)::

    python -m scripts.social_reel
    python -m scripts.social_reel --gameweek 7

Or render from a saved ``/api/scout`` response, e.g. where the API host is
not reachable from the build machine::

    python -m scripts.social_reel --data scout.json --deadline 2026-10-17T10:00:00Z

Outputs go to ``out/social/gwNN/``: a 9:16 MP4 reel, five 9:16 stills, five
4:5 carousel stills, a zip of all stills, and a ready-to-paste caption.

After the gameweek, ``--recap`` scores the locked squad against official FPL
points (``/event/{gw}/live/``) and writes a recap pack to
``out/social/gwNN_recap/``::

    python -m scripts.social_reel --recap --gameweek 6
    python -m scripts.social_reel --recap --data scout_gw06.json --live live_gw06.json --bootstrap bootstrap.json

Needs Pillow, NumPy and ffmpeg (``--no-video`` skips ffmpeg).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .data import SquadError, parse_squad

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_API = "https://openfpl.kassem.dev"
FPL_API = "https://fantasy.premierleague.com/api"


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (openfpl-social-reel/1.0)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def fetch_deadline(api: str, gameweek: int) -> Optional[str]:
    try:
        payload = _get_json(f"{api}/api/fpl/gameweeks")
    except Exception:  # The deadline is a nicety; never fail the build over it.
        return None
    for event in payload.get("results", []):
        if int(event.get("id", 0)) == gameweek:
            return event.get("deadline_time")
    return None


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--gameweek", "-g", type=int, help="Gameweek to fetch (default: the next one).")
    source.add_argument("--data", type=Path, help="Saved /api/scout JSON response to render instead of fetching.")
    parser.add_argument("--deadline", help="ISO deadline for the hook's status line (fetched when omitted).")
    parser.add_argument("--api", default=DEFAULT_API, help=f"API base URL (default {DEFAULT_API}).")
    parser.add_argument("--out", type=Path, help="Output folder (default out/social/gwNN).")
    parser.add_argument("--no-video", action="store_true", help="Only write the stills, zip and caption.")
    recap = parser.add_argument_group("recap (after the gameweek)")
    recap.add_argument("--recap", action="store_true", help="Score the locked squad against actual points.")
    recap.add_argument("--live", type=Path, help="Saved FPL /event/{gw}/live/ response (fetched when omitted).")
    recap.add_argument("--bootstrap", type=Path, help="Saved FPL /bootstrap-static/ response for the GW average.")
    args = parser.parse_args(argv)
    if args.recap and not (args.gameweek or args.data):
        parser.error("--recap needs --gameweek or --data (the next gameweek has no points yet)")

    if args.data:
        payload = json.loads(args.data.read_text(encoding="utf-8"))
    else:
        url = f"{args.api}/api/scout" + (f"?gameweek={args.gameweek}" if args.gameweek else "")
        payload = _get_json(url)

    try:
        squad = parse_squad(payload, deadline=args.deadline)
    except SquadError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.gameweek and squad.gameweek != args.gameweek:
        print(f"error: asked for GW{args.gameweek}, payload is GW{squad.gameweek}", file=sys.stderr)
        return 2
    if squad.deadline is None and not args.data:
        squad = parse_squad(payload, deadline=fetch_deadline(args.api, squad.gameweek))

    if args.recap:
        return _recap(args, squad)

    from .render import build  # Imported late so --help works without Pillow.

    out_dir = args.out or REPO_ROOT / "out" / "social" / f"gw{squad.gameweek:02d}"
    made = build(squad, out_dir, video=not args.no_video)
    print(f"GW{squad.gameweek} · captain {squad.captain.name} · vice {squad.vice.name} · frozen={squad.frozen}")
    for kind, paths in made.items():
        for path in paths:
            print(f"{kind}: {path}")
    return 0


def _recap(args: argparse.Namespace, squad) -> int:
    from .recap import RecapError, build_recap, gameweek_info, points_from_live

    gw = squad.gameweek
    live = json.loads(args.live.read_text(encoding="utf-8")) if args.live else _get_json(f"{FPL_API}/event/{gw}/live/")
    if args.bootstrap:
        boot = json.loads(args.bootstrap.read_text(encoding="utf-8"))
    else:
        try:
            boot = _get_json(f"{FPL_API}/bootstrap-static/")
        except Exception:  # The average is a nicety; never fail the build over it.
            boot = {}
    info = gameweek_info(boot, gw)
    try:
        recap = build_recap(squad, points_from_live(live), info)
    except RecapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not info.data_checked:
        print(f"warning: FPL has not confirmed GW{gw} (data_checked=false); points are provisional.", file=sys.stderr)

    from .recap_render import build_recap as render_recap

    out_dir = args.out or REPO_ROOT / "out" / "social" / f"gw{gw:02d}_recap"
    made = render_recap(recap, out_dir, video=not args.no_video)
    avg = f" · GW average {info.average}" if info.average is not None else ""
    print(f"GW{gw} recap · XI {recap.formation} · {recap.total} pts vs {recap.xp_total:.1f} xP{avg}")
    for kind, paths in made.items():
        for path in paths:
            print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

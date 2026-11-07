"""Build the OpenFPL weekly social reel and stills.

Two modes, named in each reel's hook and brand bar:

- ``--mode preview`` (default): the squad for an upcoming gameweek.
- ``--mode recap``: a finished gameweek's locked squad scored against actual points.

Fetch live (next gameweek by default, or a given one)::

    python -m scripts.social_reel
    python -m scripts.social_reel --mode preview --gameweek 7

Or render from a saved ``/api/scout`` response, e.g. where the API host is
not reachable from the build machine::

    python -m scripts.social_reel --data scout.json --deadline 2026-10-17T10:00:00Z

Outputs go to ``out/social/gwNN_preview/``: a 9:16 MP4 reel, 9:16 stills, 4:5
carousel stills, a zip of all stills, and a ready-to-paste caption. The preview
adds a differentials card (top 2 per position under 10% owned, outside the 15)
when FPL ownership is available: ``--bootstrap`` or a live /bootstrap-static/ fetch.

After the gameweek, ``--mode recap`` scores the locked squad against official FPL
points (``/event/{gw}/live/``) and writes a recap pack to
``out/social/gwNN_recap/``::

    python -m scripts.social_reel --mode recap --gameweek 6
    python -m scripts.social_reel --mode recap --data scout_gw06.json --live live_gw06.json --bootstrap bootstrap.json

Needs Pillow, NumPy and ffmpeg (``--no-video`` skips ffmpeg).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

import os

from datetime import datetime, timezone

from .data import (SquadError, differentials, differentials_from_json, differentials_to_json, ownership_from_bootstrap,
                   parse_squad, pool_rows, with_differentials)

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
    parser.add_argument("--mode", choices=("preview", "recap"), default="preview",
                        help="preview: upcoming gameweek's squad (default). recap: finished gameweek vs actual points.")
    parser.add_argument("--bootstrap", type=Path,
                        help="Saved FPL /bootstrap-static/ response (fetched when omitted): ownership for the preview's "
                             "differentials card, the GW average for the recap.")
    parser.add_argument("--pool", type=Path,
                        help="Saved /api/gw/playerpoints?gameweek=N response (the full projected pool) for the "
                             "differentials card. Defaults to the scout payload's player_points, then a live fetch "
                             "when OPENFPL_API_TOKEN is set.")
    parser.add_argument("--no-differentials", action="store_true", help="Leave the differentials card out of the preview.")
    recap = parser.add_argument_group("recap (after the gameweek)")
    recap.add_argument("--recap", action="store_true", help="Same as --mode recap.")
    recap.add_argument("--live", type=Path, help="Saved FPL /event/{gw}/live/ response (fetched when omitted).")
    recap.add_argument("--differentials", type=Path,
                       help="The preview's *_differentials.json (or a retro file in the same format) to score on a "
                            "differentials card. The live response must include those players.")
    args = parser.parse_args(argv)
    args.recap = args.recap or args.mode == "recap"
    if args.recap and not (args.gameweek or args.data):
        parser.error("recap mode needs --gameweek or --data (the next gameweek has no points yet)")

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

    if not args.no_differentials:
        squad = _attach_differentials(args, payload, squad)

    from .render import build  # Imported late so --help works without Pillow.

    out_dir = args.out or REPO_ROOT / "out" / "social" / f"gw{squad.gameweek:02d}_preview"
    made = build(squad, out_dir, video=not args.no_video)
    if squad.differentials:
        # Saved so the recap scores exactly what the preview showed, timestamped against the deadline.
        path = out_dir / f"openfpl_gw{squad.gameweek:02d}_preview_differentials.json"
        captured = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(differentials_to_json(squad, "preview", captured), ensure_ascii=False, indent=1),
                        encoding="utf-8")
        made["differentials"] = [path]
    print(f"GW{squad.gameweek} preview · captain {squad.captain.name} · vice {squad.vice.name} · frozen={squad.frozen}")
    for kind, paths in made.items():
        for path in paths:
            print(f"{kind}: {path}")
    return 0


def _attach_differentials(args: argparse.Namespace, payload: Any, squad):
    """Add the differentials card's picks; skip the card (with a warning) rather than fail the build."""
    pool_payload = json.loads(args.pool.read_text(encoding="utf-8")) if args.pool else None
    pool = pool_rows(pool_payload, payload)
    token = os.environ.get("OPENFPL_API_TOKEN")
    if not pool and token:
        try:
            request = urllib.request.Request(f"{args.api}/api/gw/playerpoints?gameweek={squad.gameweek}",
                                             headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                pool = pool_rows(json.load(response))
        except Exception as error:
            print(f"warning: could not fetch the projected pool ({error}).", file=sys.stderr)
    if not pool:
        print("warning: no projected pool (--pool / player_points); differentials card skipped.", file=sys.stderr)
        return squad
    try:
        boot = json.loads(args.bootstrap.read_text(encoding="utf-8")) if args.bootstrap else \
            _get_json(f"{FPL_API}/bootstrap-static/")
    except Exception as error:  # Ownership is a nicety; never fail the build over it.
        print(f"warning: no FPL ownership ({error}); differentials card skipped.", file=sys.stderr)
        return squad
    ownership = ownership_from_bootstrap(boot)
    if not ownership:
        print("warning: bootstrap has no elements/ownership; differentials card skipped.", file=sys.stderr)
        return squad
    picks = differentials(pool, squad, ownership)
    if not picks:
        print("warning: no player qualifies as a differential; card skipped.", file=sys.stderr)
        return squad
    print("differentials: " + " · ".join(f"{d.player.position} {d.player.name} {d.player.xp:.2f} xP {d.owned:.1f}%"
                                          for d in picks))
    return with_differentials(squad, picks)


def _recap(args: argparse.Namespace, squad) -> int:
    from .recap import RecapError, attach_differentials, build_recap, gameweek_info, points_from_live

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
    if args.differentials:
        diff_payload = json.loads(args.differentials.read_text(encoding="utf-8"))
        picks, source, captured = differentials_from_json(diff_payload)
        if int(diff_payload.get("gameweek") or gw) != gw:
            print(f"warning: differentials file is for GW{diff_payload.get('gameweek')}; card skipped.", file=sys.stderr)
        elif not picks:
            print("warning: differentials file has no picks; card skipped.", file=sys.stderr)
        else:
            try:
                recap = attach_differentials(recap, picks, points_from_live(live), source, captured)
            except RecapError as error:
                print(f"warning: {error} Differentials card skipped.", file=sys.stderr)
            else:
                if not recap.diffs_locked:
                    print("note: differentials are a retro pick (not saved by a pre-deadline preview).", file=sys.stderr)
    if not info.data_checked:
        print(f"warning: FPL has not confirmed GW{gw} (data_checked=false); points are provisional.", file=sys.stderr)

    from .recap_render import build_recap as render_recap

    out_dir = args.out or REPO_ROOT / "out" / "social" / f"gw{gw:02d}_recap"
    made = render_recap(recap, out_dir, video=not args.no_video)
    avg = f" · GW average {info.average}" if info.average is not None else ""
    print(f"GW{gw} recap · XI {recap.formation} · {recap.total} pts vs {recap.xp_total:.1f} xP{avg}")
    if recap.diffs:
        print(f"differentials ({'locked' if recap.diffs_locked else 'retro'}): {recap.diff_total} pts vs "
              f"{recap.diff_xp:.1f} xP · " + " · ".join(f"{r.player.name} {r.points}" for r in recap.diffs))
    for kind, paths in made.items():
        for path in paths:
            print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

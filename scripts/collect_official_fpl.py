"""Archive the active season from the official FPL API for future training."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from src.official_fpl import OFFICIAL_FPL_API, OfficialFPLClient


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gameweek",
        type=int,
        help="Archive matches before this gameweek (defaults to official next event)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV destination (defaults to data/official/<season>.csv)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    client = OfficialFPLClient()
    bootstrap = client.bootstrap()
    events = bootstrap["events"]
    season_finished = bool(events) and all(event.get("finished") for event in events)
    default_gameweek = 39 if season_finished else client.next_gameweek()
    target_gameweek = int(args.gameweek or default_gameweek)
    if not 1 <= target_gameweek <= 39:
        raise ValueError("--gameweek must be between 1 and 39")

    first_deadline = bootstrap["events"][0]["deadline_time"]
    season_start = int(first_deadline[:4])
    output = args.output or Path(
        f"data/official/fpl-official-{season_start}-{season_start + 1}.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    history = client.player_history(target_gameweek, selectable_only=False)
    history.to_csv(output, index=False)
    metadata = {
        "source": OFFICIAL_FPL_API,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_gameweek": target_gameweek,
        "rows": len(history),
        "players": int(history["id"].nunique()),
        "output": str(output),
    }
    output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

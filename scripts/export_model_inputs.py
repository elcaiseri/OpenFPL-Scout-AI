"""Export the exact feature rows the scout's models receive for each gameweek.

No model files are needed and nothing is archived. Each gameweek becomes
``gw_NN.csv``: one row per player fixture, ``id`` plus every model feature in
model order. ``feature_sources.csv`` labels each feature per gameweek as
``official-fpl``, ``fpl-data``, ``official-fpl+fpl-data``, ``request``, or
``missing``, with its coverage. ``summary.json`` records the strategy and FPL
Data status, or the error for a gameweek that could not be exported; the
command then exits with status 1 after exporting every other gameweek.

    uv run python -m scripts.export_model_inputs --gameweeks 1-7
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import pandas as pd
from dotenv import load_dotenv

from src.data_archive import DataArchive
from src.scout import FPLScout
from src.utils import load_config


def parse_gameweeks(value: str) -> List[int]:
    """Parse ``1-7``, ``3``, or ``1,4-6`` into sorted gameweeks."""
    gameweeks = set()
    try:
        for part in value.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                gameweeks.update(range(int(start), int(end) + 1))
            elif part:
                gameweeks.add(int(part))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid gameweeks: {value!r}") from error
    if not gameweeks or min(gameweeks) < 1 or max(gameweeks) > 38:
        raise argparse.ArgumentTypeError("gameweeks must be between 1 and 38")
    return sorted(gameweeks)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--gameweeks",
        type=parse_gameweeks,
        help="Gameweeks to export, e.g. 1-7 or 2,5 (default: the next gameweek)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/model-inputs"),
        help="Output directory (default: data/model-inputs)",
    )
    parser.add_argument("--config", default="config/config.yaml")
    return parser.parse_args(argv)


def build_scout(config: Mapping[str, Any]) -> FPLScout:
    return FPLScout(
        config,
        load_models=False,
        data_archive=DataArchive(Path("data/archive"), enabled=False),
    )


def main(
    argv: Optional[Iterable[str]] = None,
    scout_factory: Callable[[Mapping[str, Any]], FPLScout] = build_scout,
) -> int:
    load_dotenv()
    args = parse_args(argv)
    scout = scout_factory(load_config(args.config))
    gameweeks = args.gameweeks or [scout.official_client.next_gameweek()]
    args.output.mkdir(parents=True, exist_ok=True)

    summary: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    failed = 0
    for gameweek in gameweeks:
        path = args.output / f"gw_{gameweek:02d}.csv"
        try:
            frame, metadata = scout.export_model_inputs(gameweek)
        except Exception as error:  # Report it and keep exporting the rest.
            failed += 1
            path.unlink(missing_ok=True)
            summary.append({"gameweek": gameweek, "error": str(error)})
            print(f"GW{gameweek}: failed: {error}")
            continue
        frame.to_csv(path, index=False)
        enrichment = metadata["data_enrichment"]
        for feature, source in metadata["feature_sources"].items():
            sources.append(
                {
                    "gameweek": gameweek,
                    "feature": feature,
                    "source": source,
                    "coverage": round(float(frame[feature].notna().mean()), 4),
                }
            )
        summary.append(
            {
                "gameweek": gameweek,
                "strategy": metadata["strategy"],
                "rows": metadata["rows"],
                "players": metadata["players"],
                "fpl_data_status": enrichment.get("status"),
                "unenriched_gameweeks": enrichment.get("unenriched_gameweeks", []),
                "fpl_data_error": enrichment.get("error"),
                "file": str(path),
            }
        )
        print(
            f"GW{gameweek}: {metadata['strategy']}, {metadata['rows']} rows for "
            f"{metadata['players']} players, FPL Data {enrichment.get('status')} "
            f"-> {path}"
        )

    source_table = pd.DataFrame(
        sources, columns=["gameweek", "feature", "source", "coverage"]
    )
    source_table.to_csv(args.output / "feature_sources.csv", index=False)
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    if not source_table.empty:
        last_gameweek = source_table["gameweek"].max()
        latest = source_table.loc[source_table["gameweek"] == last_gameweek]
        print(f"\nFeature sources for GW{int(latest['gameweek'].iloc[0])}:")
        for source, group in latest.groupby("source", sort=False):
            print(f"  {source} ({len(group)}): {', '.join(group['feature'])}")
    print(
        f"\nWrote {len(gameweeks) - failed} of {len(gameweeks)} gameweek file(s) "
        f"to {args.output}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

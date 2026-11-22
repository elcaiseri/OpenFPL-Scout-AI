"""Compare mean and median ensembles on out-of-fold model predictions.

The input holds one row per player fixture, with each model's prediction in
its own column, the actual points, and the gameweek. Predictions must be out
of fold (or from a holdout season): in-sample predictions favour whichever
combination overfits most.

Required columns: one per model (``--models``), the target (``--target``,
default ``total_points``) and ``gameweek``. Optional: ``season`` (gameweeks of
different seasons are kept apart), ``id`` (double gameweeks are summed per
player before ranking; without it every row counts as a player),
``element_type`` (1-4; enables the squad backtest), ``team_name`` (applies the
production limit of three players per club).

The squad backtest mirrors the technical report's: each gameweek, fill 2 GK,
5 DEF, 5 MID and 3 FWD from the highest forecast down, score the actual points
of all 15, and count the captain (the top forecast) twice.

Differences are median minus mean. Their 95% intervals come from a paired
bootstrap over gameweeks, which keeps each gameweek's players together.

Example:
    python -m scripts.compare_ensemble_methods oof.csv \\
        --models linear_regression,xgboost,catboost,mlp
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from src.scout import ENSEMBLE_METHODS, combine_predictions

SQUAD_QUOTA = {1: 2, 2: 5, 3: 5, 4: 3}
POSITION_NAMES = {
    "goalkeeper": 1, "gk": 1, "gkp": 1,
    "defender": 2, "def": 2,
    "midfielder": 3, "mid": 3,
    "forward": 4, "fwd": 4,
}


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("predictions", type=Path, help="CSV or Parquet file")
    parser.add_argument(
        "--models",
        required=True,
        help="Comma-separated prediction columns, one per model",
    )
    parser.add_argument(
        "--weights",
        help="Comma-separated name=weight pairs; unlisted models weigh 1. "
        "Without this every model weighs 1, as in production today.",
    )
    parser.add_argument("--target", default="total_points")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--max-per-club", type=int, default=3)
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--disagreement-quantile",
        type=float,
        default=0.9,
        help="Also score rows whose model spread is above this quantile",
    )
    parser.add_argument("--json", type=Path, help="Also write the results here")
    return parser.parse_args(argv)


def load_predictions(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    return pd.read_csv(path)


def parse_weights(models: Sequence[str], text: Optional[str]) -> np.ndarray:
    weights = {name: 1.0 for name in models}
    for item in filter(None, (part.strip() for part in (text or "").split(","))):
        name, _, value = item.partition("=")
        if name not in weights:
            raise ValueError(f"--weights names unknown model {name!r}")
        weight = float(value)
        if not np.isfinite(weight) or weight <= 0:
            raise ValueError(f"Weight for {name!r} must be a positive number")
        weights[name] = weight
    return np.asarray([weights[name] for name in models], dtype=float)


def _rank_correlation(left: pd.Series, right: pd.Series) -> float:
    """Spearman correlation without SciPy: Pearson on average ranks."""
    if len(left) < 2:
        return float("nan")
    ranked_left = left.rank().to_numpy(dtype=float)
    ranked_right = right.rank().to_numpy(dtype=float)
    if ranked_left.std() == 0 or ranked_right.std() == 0:
        return float("nan")
    return float(np.corrcoef(ranked_left, ranked_right)[0, 1])


def select_squad(
    players: pd.DataFrame, score: str, max_per_club: Optional[int]
) -> Optional[pd.DataFrame]:
    """Fill the positional quota greedily, as the production selector does."""
    ranked = players.sort_values(score, ascending=False, kind="stable")
    counts = {position: 0 for position in SQUAD_QUOTA}
    clubs: Dict[str, int] = {}
    chosen: List[int] = []
    use_clubs = max_per_club is not None and "team_name" in ranked.columns
    for index, position, club in zip(
        ranked.index,
        ranked["element_type"].to_numpy(),
        ranked["team_name"].to_numpy() if use_clubs else [None] * len(ranked),
    ):
        if position not in SQUAD_QUOTA or counts[position] >= SQUAD_QUOTA[position]:
            continue
        if use_clubs and clubs.get(club, 0) >= max_per_club:
            continue
        chosen.append(index)
        counts[position] += 1
        if use_clubs:
            clubs[club] = clubs.get(club, 0) + 1
        if len(chosen) == sum(SQUAD_QUOTA.values()):
            return ranked.loc[chosen]
    return None


def _gameweek_scores(
    rows: pd.DataFrame,
    players: pd.DataFrame,
    methods: Sequence[str],
    target: str,
    top_n: int,
    max_per_club: Optional[int],
) -> pd.DataFrame:
    """One row per gameweek: error sums and ranking and squad results."""
    records = []
    row_groups = dict(tuple(rows.groupby("_gw_key", sort=True)))
    has_positions = "element_type" in players.columns
    for key, week in players.groupby("_gw_key", sort=True):
        week_rows = row_groups[key]
        record: Dict[str, float] = {"_gw_key": key, "rows": len(week_rows)}
        actual_top = set(week.nlargest(top_n, target).index)
        for method in methods:
            error = week_rows[method] - week_rows[target]
            record[f"{method}:sse"] = float((error**2).sum())
            record[f"{method}:sae"] = float(error.abs().sum())
            record[f"{method}:spearman"] = _rank_correlation(week[method], week[target])
            predicted_top = set(week.nlargest(top_n, method).index)
            record[f"{method}:top_n"] = len(actual_top & predicted_top) / max(
                min(top_n, len(week)), 1
            )
            if has_positions:
                squad = select_squad(week, method, max_per_club)
                if squad is None:
                    record[f"{method}:squad"] = float("nan")
                    record[f"{method}:captain"] = float("nan")
                else:
                    captain = float(squad[target].iloc[0])
                    record[f"{method}:captain"] = captain
                    record[f"{method}:squad"] = float(squad[target].sum()) + captain
        records.append(record)
    return pd.DataFrame(records)


def _summarise(scores: pd.DataFrame, method: str) -> Dict[str, float]:
    rows = scores["rows"].sum()
    summary = {
        "rmse": float(np.sqrt(scores[f"{method}:sse"].sum() / rows)),
        "mae": float(scores[f"{method}:sae"].sum() / rows),
        "spearman_per_gw": float(scores[f"{method}:spearman"].mean()),
        "top_n_hit_rate": float(scores[f"{method}:top_n"].mean()),
    }
    if f"{method}:squad" in scores:
        summary["squad_points_per_gw"] = float(scores[f"{method}:squad"].mean())
        summary["captain_points_per_gw"] = float(scores[f"{method}:captain"].mean())
    return summary


def _bootstrap_difference(
    scores: pd.DataFrame, left: str, right: str, draws: int, seed: int
) -> Dict[str, Dict[str, float]]:
    """Paired bootstrap over gameweeks of ``left`` minus ``right``."""
    rng = np.random.default_rng(seed)
    weeks = len(scores)
    samples = rng.integers(0, weeks, size=(draws, weeks)) if weeks else None
    rows = scores["rows"].to_numpy(dtype=float)

    def interval(statistic) -> Dict[str, float]:
        point = float(statistic(np.arange(weeks)))
        if samples is None:
            return {"difference": point, "low": float("nan"), "high": float("nan")}
        drawn = np.array([statistic(sample) for sample in samples])
        drawn = drawn[np.isfinite(drawn)]
        low, high = (
            np.percentile(drawn, [2.5, 97.5]) if len(drawn) else (np.nan, np.nan)
        )
        return {"difference": point, "low": float(low), "high": float(high)}

    def ratio(column: str, method: str, sample: np.ndarray) -> float:
        return scores[f"{method}:{column}"].to_numpy()[sample].sum() / rows[sample].sum()

    result = {
        "rmse": interval(
            lambda s: np.sqrt(ratio("sse", left, s)) - np.sqrt(ratio("sse", right, s))
        ),
        "mae": interval(lambda s: ratio("sae", left, s) - ratio("sae", right, s)),
        "spearman_per_gw": interval(
            lambda s: np.nanmean(scores[f"{left}:spearman"].to_numpy()[s])
            - np.nanmean(scores[f"{right}:spearman"].to_numpy()[s])
        ),
        "top_n_hit_rate": interval(
            lambda s: scores[f"{left}:top_n"].to_numpy()[s].mean()
            - scores[f"{right}:top_n"].to_numpy()[s].mean()
        ),
    }
    if f"{left}:squad" in scores:
        result["squad_points_per_gw"] = interval(
            lambda s: np.nanmean(
                scores[f"{left}:squad"].to_numpy()[s]
                - scores[f"{right}:squad"].to_numpy()[s]
            )
        )
        difference = scores[f"{left}:squad"] - scores[f"{right}:squad"]
        result["squad_gameweeks"] = {
            "won": int((difference > 0).sum()),
            "tied": int((difference == 0).sum()),
            "lost": int((difference < 0).sum()),
        }
    return result


def compare(
    frame: pd.DataFrame,
    models: Sequence[str],
    weights: np.ndarray,
    target: str = "total_points",
    top_n: int = 20,
    max_per_club: Optional[int] = 3,
    bootstrap: int = 2000,
    seed: int = 42,
    disagreement_quantile: float = 0.9,
) -> Dict[str, object]:
    missing = [c for c in (*models, target, "gameweek") if c not in frame.columns]
    if missing:
        raise ValueError(f"Predictions are missing columns: {missing}")
    rows = frame.dropna(subset=[*models, target, "gameweek"]).copy()
    if rows.empty:
        raise ValueError("No rows have every model's prediction and the target")

    matrix = rows[list(models)].to_numpy(dtype=float).T
    methods = list(ENSEMBLE_METHODS)
    for method in methods:
        rows[method] = combine_predictions(matrix, weights, method)
    rows["_spread"] = matrix.std(axis=0)
    rows["_gw_key"] = (
        rows["season"].astype(str) + "-GW" + rows["gameweek"].astype(int).astype(str)
        if "season" in rows.columns
        else "GW" + rows["gameweek"].astype(int).astype(str)
    )

    # A double gameweek is two fixture rows; rank players on their total.
    if "id" in rows.columns:
        keys = ["_gw_key", "id"]
        extra = {
            column: "first"
            for column in ("element_type", "team_name")
            if column in rows.columns
        }
        players = rows.groupby(keys, sort=False).agg(
            {**{c: "sum" for c in (*methods, target)}, **extra}
        )
        players = players.reset_index()
    else:
        players = rows
    if "element_type" in players.columns:
        positions = players["element_type"]
        named = positions.astype(str).str.strip().str.casefold().map(POSITION_NAMES)
        players["element_type"] = pd.to_numeric(positions, errors="coerce").fillna(
            named
        )

    club_limit = max_per_club if "team_name" in players.columns else None
    scores = _gameweek_scores(rows, players, methods, target, top_n, club_limit)
    results: Dict[str, object] = {
        "rows": int(len(rows)),
        "gameweeks": int(len(scores)),
        "models": list(models),
        "weights": dict(zip(models, weights.tolist())),
        "rows_where_methods_differ": int(
            (~np.isclose(rows["mean"], rows["median"])).sum()
        ),
        "per_club_limit": club_limit,
        "methods": {method: _summarise(scores, method) for method in methods},
        "median_minus_mean": _bootstrap_difference(
            scores, "median", "mean", bootstrap, seed
        ),
    }

    threshold = float(rows["_spread"].quantile(disagreement_quantile))
    disputed = rows.loc[rows["_spread"] >= threshold]
    results["high_disagreement"] = {
        "quantile": disagreement_quantile,
        "spread_at_least": threshold,
        "rows": int(len(disputed)),
        **{
            method: {
                "rmse": float(
                    np.sqrt(((disputed[method] - disputed[target]) ** 2).mean())
                ),
                "mae": float((disputed[method] - disputed[target]).abs().mean()),
                "bias": float((disputed[method] - disputed[target]).mean()),
            }
            for method in methods
        },
    }

    top_decile = rows["mean"] >= rows["mean"].quantile(0.9)
    results["top_decile_calibration"] = {
        "actual": float(rows.loc[top_decile, target].mean()),
        **{method: float(rows.loc[top_decile, method].mean()) for method in methods},
    }
    return results


def _format(results: Mapping[str, object]) -> str:
    methods = results["methods"]
    lines = [
        f"{results['rows']} rows over {results['gameweeks']} gameweeks; "
        f"weights {results['weights']}",
        f"Rows where mean and median differ: {results['rows_where_methods_differ']}",
        "Per-club limit: "
        + (
            str(results["per_club_limit"])
            if results["per_club_limit"] is not None
            else "not applied (no team_name column)"
        ),
        "",
        f"{'metric':<24}{'mean':>12}{'median':>12}"
        f"{'median-mean':>14}{'95% interval':>24}",
    ]
    differences = results["median_minus_mean"]
    for metric in methods["mean"]:
        if metric == "captain_points_per_gw":
            continue
        interval = differences.get(metric, {})
        lines.append(
            f"{metric:<24}{methods['mean'][metric]:>12.4f}"
            f"{methods['median'][metric]:>12.4f}"
            f"{interval.get('difference', float('nan')):>+14.4f}"
            f"{'[%+.4f, %+.4f]' % (interval.get('low', np.nan), interval.get('high', np.nan)):>24}"
        )
    if "captain_points_per_gw" in methods["mean"]:
        lines.append(
            f"{'captain_points_per_gw':<24}"
            f"{methods['mean']['captain_points_per_gw']:>12.4f}"
            f"{methods['median']['captain_points_per_gw']:>12.4f}"
        )
    if "squad_gameweeks" in differences:
        record = differences["squad_gameweeks"]
        lines.append(
            f"Median squad vs mean squad: won {record['won']}, "
            f"tied {record['tied']}, lost {record['lost']} gameweeks"
        )
    disputed = results["high_disagreement"]
    lines += [
        "",
        f"Rows where the models disagree most (spread >= "
        f"{disputed['spread_at_least']:.3f}, {disputed['rows']} rows):",
    ]
    for method in ENSEMBLE_METHODS:
        scores = disputed[method]
        lines.append(
            f"  {method:<8} rmse {scores['rmse']:.4f}  mae {scores['mae']:.4f}  "
            f"bias {scores['bias']:+.4f}"
        )
    calibration = results["top_decile_calibration"]
    lines += [
        "",
        f"Top decile (by mean forecast): actual {calibration['actual']:.3f}, "
        f"mean {calibration['mean']:.3f}, median {calibration['median']:.3f}",
    ]
    return "\n".join(lines)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    models = [name.strip() for name in args.models.split(",") if name.strip()]
    if len(models) < 2:
        raise ValueError("--models needs at least two prediction columns")
    results = compare(
        load_predictions(args.predictions),
        models,
        parse_weights(models, args.weights),
        target=args.target,
        top_n=args.top_n,
        max_per_club=args.max_per_club if args.max_per_club > 0 else None,
        bootstrap=args.bootstrap,
        seed=args.seed,
        disagreement_quantile=args.disagreement_quantile,
    )
    print(_format(results))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

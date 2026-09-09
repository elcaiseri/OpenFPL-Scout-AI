"""Read recorded training evaluations; no retraining or invented historical forecasts."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from threading import RLock

import pandas as pd

from src.observatory import calibration, point_metrics
from src.data_archive import _json_safe


class ModelLab:
    def __init__(self, config):
        self.config = config
        self.cache = {}
        self.lock = RLock()

    def report(self, dataset="holdout"):
        if dataset not in {"holdout", "cross-validation"}:
            raise ValueError("Unknown evaluation dataset")
        with self.lock:
            cached = self.cache.get(dataset)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            result = self._read(dataset)
            self.cache[dataset] = (time.monotonic(), result)
            return result

    def _read(self, dataset):
        roots = list(dict.fromkeys(Path(str(m["path"])).parent for m in self.config.get("models", {}).values() if m.get("path")))
        filename = "holdout_predictions.csv" if dataset == "holdout" else "oof_predictions.csv"
        root = next((p for p in roots if (p / filename).is_file()), None)
        if root is None:
            return {"available": False, "dataset": dataset, "models": [], "message": f"No {filename} artifact is available beside the configured models."}
        metadata = {}
        warnings = []
        try:
            metadata = json.loads((root / "training_metadata.json").read_text())
        except (OSError, ValueError):
            warnings.append("Training metadata is unavailable; evaluation file provenance is limited to its path.")
        try:
            frame = pd.read_csv(root / filename)
            if "actual" not in frame:
                raise ValueError("The evaluation artifact has no actual column")
        except (OSError, ValueError) as error:
            return {"available": False, "dataset": dataset, "models": [], "message": str(error)}
        candidates = set(self.config.get("models", {})) | {"equal_ensemble", "weighted_ensemble"}
        columns = [c for c in frame if c in candidates or c.startswith("baseline_")]
        models = []
        actual = pd.to_numeric(frame.actual, errors="coerce")
        for name in columns:
            values = pd.to_numeric(frame[name], errors="coerce")
            valid = pd.DataFrame({"expected_points": values, "actual_points": actual}).replace([float("inf"), -float("inf")], float("nan")).dropna()
            records = valid.to_dict("records")
            metrics = point_metrics(records)
            timeline = []
            if "gameweek" in frame and "season" in frame:
                for (season, gw), indices in frame.groupby(["season", "gameweek"]).groups.items():
                    group = valid.loc[valid.index.intersection(indices)]
                    timeline.append({"season": str(season), "gameweek": int(gw), **point_metrics(group.to_dict("records"))})
            sample = valid.iloc[::max(1, math.ceil(len(valid) / 300))]
            errors = (valid.expected_points - valid.actual_points).abs().sort_values(ascending=False).head(10)
            outliers = [{"name": str(frame.loc[i, "web_name"]) if "web_name" in frame else str(i), "gameweek": int(frame.loc[i, "gameweek"]) if "gameweek" in frame else None, **valid.loc[i].to_dict()} for i in errors.index]
            models.append({
                "name": name, "kind": "baseline" if name.startswith("baseline_") else "ensemble" if "ensemble" in name else "model",
                **metrics, "calibration": calibration(records), "timeline": timeline,
                "scatter": sample.to_dict("records"), "outliers": outliers,
                "cv": metadata.get("models", {}).get(name, {}),
            })
        models.sort(key=lambda m: (m["mae"] is None, m["mae"] or 0))
        return _json_safe({
            "available": True, "dataset": dataset, "rows": len(frame),
            "source": str(root / filename), "trained_at_utc": metadata.get("trained_at_utc"),
            "validation_strategy": metadata.get("validation_strategy"),
            "dataset_seasons": metadata.get("dataset", {}).get("seasons", []),
            "evaluation_seasons": sorted(str(s) for s in frame.season.unique()) if "season" in frame else [],
            "feature_count": metadata.get("dataset", {}).get("feature_count"),
            "features": metadata.get("dataset", {}).get("features", []),
            "folds": metadata.get("fold_results", []), "models": models, "warnings": warnings,
            "note": "Metrics use recorded evaluation rows and their actual labels. Dataset season identifiers are preserved as stored. These results are separate from live-season accuracy and may use different populations and model versions.",
        })

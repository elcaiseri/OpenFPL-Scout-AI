import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.compare_ensemble_methods import (
    compare,
    main,
    parse_weights,
    select_squad,
)

MODELS = ["ridge", "xgb", "cat", "mlp"]


def synthetic_predictions(outliers=True, seed=0):
    """Four noisy models; "mlp" sometimes overshoots wildly when outliers."""
    rng = np.random.default_rng(seed)
    rows = []
    for gameweek in range(1, 9):
        for player in range(1, 81):
            truth = rng.gamma(1.2, 1.5)
            row = {
                "season": 2026,
                "gameweek": gameweek,
                "id": player,
                "element_type": 1 + player % 4,
                "team_name": f"Club {player % 10}",
                "total_points": truth + rng.normal(0, 1.0),
            }
            for model in MODELS:
                row[model] = truth + rng.normal(0, 0.4)
            if outliers and rng.random() < 0.15:
                row["mlp"] += 12.0
            rows.append(row)
    return pd.DataFrame(rows)


class CompareEnsembleMethodsTests(unittest.TestCase):
    def test_median_wins_when_one_model_has_outliers(self):
        results = compare(
            synthetic_predictions(), MODELS, np.ones(4), bootstrap=200
        )

        difference = results["median_minus_mean"]
        self.assertLess(difference["rmse"]["high"], 0)
        self.assertLess(difference["mae"]["high"], 0)
        self.assertGreater(
            results["methods"]["median"]["squad_points_per_gw"],
            results["methods"]["mean"]["squad_points_per_gw"],
        )
        self.assertEqual(results["gameweeks"], 8)
        self.assertEqual(results["per_club_limit"], 3)
        record = difference["squad_gameweeks"]
        self.assertEqual(record["won"] + record["tied"] + record["lost"], 8)

    def test_methods_agree_without_outliers_in_two_models(self):
        frame = synthetic_predictions(outliers=False)
        frame["xgb"] = frame["ridge"]

        results = compare(frame, ["ridge", "xgb"], np.ones(2), bootstrap=50)

        self.assertEqual(results["rows_where_methods_differ"], 0)
        self.assertAlmostEqual(results["median_minus_mean"]["rmse"]["difference"], 0)

    def test_double_gameweeks_rank_players_on_their_total(self):
        frame = synthetic_predictions(outliers=False)
        double = frame.loc[frame["id"] == 1].copy()
        frame = pd.concat([frame, double], ignore_index=True)

        results = compare(frame, MODELS, np.ones(4), bootstrap=10)

        self.assertEqual(results["rows"], 8 * 81)

    def test_squad_fills_the_quota_within_the_club_limit(self):
        players = synthetic_predictions(outliers=False).query("gameweek == 1")

        squad = select_squad(players, "cat", max_per_club=3)

        self.assertEqual(len(squad), 15)
        self.assertEqual(
            squad["element_type"].value_counts().sort_index().tolist(), [2, 5, 5, 3]
        )
        self.assertLessEqual(squad["team_name"].value_counts().max(), 3)
        self.assertEqual(squad["cat"].iloc[0], players["cat"].max())

    def test_weights_and_missing_columns_are_validated(self):
        self.assertEqual(parse_weights(MODELS, "cat=2").tolist(), [1, 1, 2, 1])
        with self.assertRaisesRegex(ValueError, "unknown model"):
            parse_weights(MODELS, "gbm=2")
        with self.assertRaisesRegex(ValueError, "missing columns"):
            compare(synthetic_predictions().drop(columns="mlp"), MODELS, np.ones(4))

    def test_command_line_prints_and_writes_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "oof.csv"
            output = Path(directory) / "results.json"
            synthetic_predictions().to_csv(source, index=False)

            code = main(
                [str(source), "--models", ",".join(MODELS), "--bootstrap", "50",
                 "--json", str(output)]
            )

            self.assertEqual(code, 0)
            self.assertIn("median_minus_mean", json.loads(output.read_text()))


if __name__ == "__main__":
    unittest.main()

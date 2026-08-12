import unittest

import numpy as np
import pandas as pd

from src.features import (
    MODEL_FEATURES,
    add_rolling_history,
    ensure_feature_columns,
    normalize_fpl_columns,
)


class FeaturePreparationTests(unittest.TestCase):
    def test_normalizes_legacy_stats_and_team_names(self):
        source = pd.DataFrame(
            {
                "shots": [3.0],
                "xG": [0.7],
                "Att Pen": [5.0],
                "team_name": ["Man Utd"],
                "opponent_team_name": ["Spurs"],
            }
        )

        result = normalize_fpl_columns(source)

        self.assertEqual(result.loc[0, "total_shots"], 3.0)
        self.assertEqual(result.loc[0, "expected_goals"], 0.7)
        self.assertEqual(result.loc[0, "touches_opp_box"], 5.0)
        self.assertEqual(result.loc[0, "team_name"], "Manchester United")
        self.assertEqual(result.loc[0, "opponent_team_name"], "Tottenham")
        self.assertNotIn("shots", result.columns)

    def test_rolling_history_never_uses_current_match(self):
        source = pd.DataFrame(
            {
                "_season": [2026, 2026, 2026],
                "id": [10, 10, 10],
                "web_name": ["Player", "Player", "Player"],
                "gameweek": [1, 2, 3],
                "goals": [1, 3, 8],
            }
        )

        result = add_rolling_history(source, window=2)

        self.assertTrue(np.isnan(result.loc[result.gameweek == 1, "goals"].iloc[0]))
        self.assertEqual(result.loc[result.gameweek == 2, "goals"].iloc[0], 1.0)
        self.assertEqual(result.loc[result.gameweek == 3, "goals"].iloc[0], 2.0)

    def test_double_gameweek_fixtures_share_pre_gameweek_history(self):
        source = pd.DataFrame(
            {
                "_season": [2026, 2026, 2026],
                "id": [10, 10, 10],
                "web_name": ["Player", "Player", "Player"],
                "gameweek": [1, 2, 2],
                "goals": [1, 4, 9],
            }
        )

        result = add_rolling_history(source, window=5)
        double_gameweek = result.loc[result.gameweek == 2, "goals"]

        self.assertEqual(double_gameweek.tolist(), [1.0, 1.0])

    def test_model_contract_adds_and_orders_missing_features(self):
        result = ensure_feature_columns(pd.DataFrame({"gameweek": [1]}))

        self.assertEqual(list(result.columns), MODEL_FEATURES)
        self.assertTrue(result.drop(columns="gameweek").isna().all().all())


if __name__ == "__main__":
    unittest.main()

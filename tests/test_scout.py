import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.features import MODEL_FEATURES
from src.scout import FPLScout, InferenceError


class ConstantModel:
    feature_names_in_ = np.asarray(MODEL_FEATURES)

    def __init__(self, value=1.0, error=None):
        self.value = value
        self.error = error

    def predict(self, features):
        if self.error:
            raise RuntimeError(self.error)
        return np.full(len(features), self.value)


class WrongContractModel(ConstantModel):
    feature_names_in_ = np.asarray(["wrong_feature"])


class RecordingModel(ConstantModel):
    def predict(self, features):
        self.features = features.copy()
        return super().predict(features)


class FakeOfficialClient:
    def next_gameweek(self):
        return 2

    def player_history(self, gameweek):
        history = player_history()
        if gameweek == 1:
            history = history.drop_duplicates("id").copy()
            history["gameweek"] = 0
            return history
        return history.loc[history["gameweek"] < gameweek].copy()


class FakeFPLDataProvider:
    def __init__(self):
        self.calls = []

    def enrich(self, history, gameweek):
        self.calls.append(gameweek)
        enriched = history.copy()
        enriched["total_shots"] = 7.0
        return enriched, {
            "provider": "fpl-data",
            "status": "applied",
            "matched_rows": len(enriched),
        }


def player_history():
    rows = []
    for player_id in range(1, 21):
        if player_id <= 2:
            position = 1
        elif player_id <= 7:
            position = 2
        elif player_id <= 12:
            position = 3
        else:
            position = 4
        for gameweek in (1, 2):
            rows.append(
                {
                    "id": player_id,
                    "element_type": position,
                    "web_name": f"Player {player_id}",
                    "team_name": "Arsenal",
                    "opponent_team_name": "Chelsea",
                    "was_home": True,
                    "gameweek": gameweek,
                    "goals": player_id / 10,
                }
            )
    return pd.DataFrame(rows)


def scout_config(minimum=3):
    return {
        "categorical_columns": [
            "id",
            "element_type",
            "web_name",
            "team_name",
            "opponent_team_name",
            "was_home",
            "gameweek",
        ],
        "team_name_mapping": {},
        "gw_team_name_mapping": {},
        "inference": {
            "history_window": 5,
            "minimum_successful_models": minimum,
            "clip_min": 0.0,
            "cache_fixtures": True,
        },
        "models": {
            "one": {"path": "one.pkl", "weight": 1},
            "two": {"path": "two.pkl", "weight": 1},
            "three": {"path": "three.pkl", "weight": 2},
            "four": {"path": "four.pkl", "weight": 1},
        },
    }


def enable_fpl_data(config):
    config["fpl_data_inference"] = {
        "enabled": True,
        "start_gameweek": 2,
        "season": "2026_27",
        "permission_status": "pending",
    }
    return config


class ScoutInferenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture_calls = 0

        def fixtures(gameweek, mapping):
            self.fixture_calls += 1
            return {
                "Arsenal": {
                    "opponent_team_name": "Chelsea",
                    "was_home": True,
                }
            }

        self.fixtures = fixtures

    def test_weighted_ensemble_preserves_output_contract_and_caches_fixtures(self):
        models = {
            "one.pkl": ConstantModel(1),
            "two.pkl": ConstantModel(2),
            "three.pkl": ConstantModel(3),
            "four.pkl": ConstantModel(4),
        }
        scout = FPLScout(
            scout_config(),
            fixture_provider=self.fixtures,
            model_loader=models.__getitem__,
        )

        first = scout.predict_players(player_history(), gameweek=3)
        second = scout.predict_players(player_history(), gameweek=3)

        self.assertEqual(len(first), 20)
        self.assertTrue(np.allclose(first.expected_points, 2.6))
        self.assertEqual(first.attrs["gameweek"], 3)
        self.assertEqual(first.attrs["inference"]["failed_models"], {})
        expected_columns = scout_config()["categorical_columns"] + ["expected_points"]
        self.assertEqual(list(first.columns), expected_columns)
        self.assertEqual(self.fixture_calls, 1)
        self.assertTrue(first.equals(second))

    def test_one_failed_model_can_degrade_without_failing_request(self):
        models = {
            "one.pkl": ConstantModel(1),
            "two.pkl": ConstantModel(2),
            "three.pkl": ConstantModel(error="broken"),
            "four.pkl": ConstantModel(4),
        }
        scout = FPLScout(
            scout_config(minimum=3),
            fixture_provider=self.fixtures,
            model_loader=models.__getitem__,
        )

        result = scout.predict_players(player_history(), gameweek=3)

        self.assertTrue(np.allclose(result.expected_points, 7 / 3))
        self.assertEqual(
            result.attrs["inference"]["failed_models"], {"three": "broken"}
        )

    def test_fails_when_too_few_models_succeed(self):
        models = {
            "one.pkl": ConstantModel(1),
            "two.pkl": ConstantModel(error="broken two"),
            "three.pkl": ConstantModel(error="broken three"),
            "four.pkl": ConstantModel(4),
        }
        scout = FPLScout(
            scout_config(minimum=3),
            fixture_provider=self.fixtures,
            model_loader=models.__getitem__,
        )

        with self.assertRaisesRegex(InferenceError, "Only 2 of 4 models succeeded"):
            scout.predict_players(player_history(), gameweek=3)

    def test_team_selection_validates_position_depth(self):
        scout = FPLScout(
            scout_config(),
            fixture_provider=self.fixtures,
            model_loader=lambda path: ConstantModel(),
        )
        insufficient = pd.DataFrame(
            {
                "id": [1],
                "element_type": [1],
                "web_name": ["Only keeper"],
                "expected_points": [4.0],
            }
        )

        with self.assertRaisesRegex(ValueError, "Need 2 position-1 players"):
            scout.select_optimal_team(insufficient)

    def test_rejects_model_with_stale_feature_contract(self):
        with self.assertRaisesRegex(InferenceError, "different feature contract"):
            FPLScout(
                scout_config(),
                fixture_provider=self.fixtures,
                model_loader=lambda path: WrongContractModel(),
            )

    def test_clips_negative_ensemble_predictions(self):
        scout = FPLScout(
            scout_config(),
            fixture_provider=self.fixtures,
            model_loader=lambda path: ConstantModel(-2),
        )

        result = scout.predict_players(player_history(), gameweek=3)

        self.assertTrue((result.expected_points == 0).all())

    def test_official_predictions_use_fpl_data_after_gameweek_one(self):
        provider = FakeFPLDataProvider()
        model = RecordingModel(2)
        scout = FPLScout(
            enable_fpl_data(scout_config()),
            fixture_provider=self.fixtures,
            model_loader=lambda path: model,
            official_client=FakeOfficialClient(),
            fpl_data_provider=provider,
        )

        result = scout.get_official_predictions(gameweek=2)

        self.assertEqual(provider.calls, [2])
        self.assertTrue((model.features["total_shots"] == 7).all())
        self.assertEqual(result.attrs["source"], "official-fpl+fpl-data")
        self.assertEqual(
            result.attrs["inference"]["data_enrichment"]["status"], "applied"
        )

    def test_gameweek_one_does_not_contact_fpl_data(self):
        provider = FakeFPLDataProvider()
        scout = FPLScout(
            enable_fpl_data(scout_config()),
            fixture_provider=self.fixtures,
            model_loader=lambda path: ConstantModel(2),
            official_client=FakeOfficialClient(),
            fpl_data_provider=provider,
        )

        result = scout.get_official_predictions(gameweek=1)

        self.assertEqual(provider.calls, [])
        self.assertEqual(result.attrs["source"], "official-fpl")
        self.assertEqual(
            result.attrs["inference"]["data_enrichment"]["status"],
            "before-start-gameweek",
        )

    def test_environment_kill_switch_disables_enrichment(self):
        provider = FakeFPLDataProvider()
        with patch.dict(
            "os.environ", {"FPL_DATA_INFERENCE_ENABLED": "false"}, clear=False
        ):
            scout = FPLScout(
                enable_fpl_data(scout_config()),
                fixture_provider=self.fixtures,
                model_loader=lambda path: ConstantModel(2),
                official_client=FakeOfficialClient(),
                fpl_data_provider=provider,
            )

        result = scout.get_official_predictions(gameweek=2)

        self.assertEqual(provider.calls, [])
        self.assertEqual(result.attrs["source"], "official-fpl")
        self.assertEqual(
            result.attrs["inference"]["data_enrichment"]["status"], "disabled"
        )


if __name__ == "__main__":
    unittest.main()

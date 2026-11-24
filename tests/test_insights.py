"""Selection traces, explanations, accuracy and forecast diffs for the dashboard."""

import unittest

import numpy as np
import pandas as pd

from src import insights
from src.scout import FPLScout
from tests.test_scout import (
    ConstantModel,
    GoalsModel,
    VenueModel,
    league_history,
    scout_config,
)


def league_fixtures(gameweek, mapping):
    """Seven clubs: club 0 at home to Chelsea, the rest away."""
    return {
        f"Club {club}": {
            "fixtures": [
                {
                    "opponent_team_name": "Chelsea",
                    "was_home": club == 0,
                    "difficulty": 2 if club == 0 else 4,
                }
            ]
        }
        for club in range(7)
    }


def build_scout(models, fixture_provider=league_fixtures, minimum=1):
    config = scout_config(minimum=minimum)
    config["models"] = {name: {"path": name} for name in models}
    return FPLScout(
        config,
        fixture_provider=fixture_provider,
        model_loader=models.__getitem__,
    )


def random_predictions(seed, players=120, clubs=8):
    rng = np.random.default_rng(seed)
    positions = rng.choice([1, 2, 3, 4], size=players, p=[0.12, 0.33, 0.35, 0.2])
    frame = pd.DataFrame(
        {
            "id": np.arange(1, players + 1),
            "element_type": positions,
            "web_name": [f"P{index}" for index in range(1, players + 1)],
            "team_name": [f"Club {value}" for value in rng.integers(0, clubs, players)],
            # Rounded so ties exercise the captain ordering.
            "expected_points": np.round(rng.gamma(2.0, 1.5, players), 1),
            "availability_factor": rng.choice([1.0, 1.0, 1.0, 0.5, 0.0], players),
        }
    )
    frame.attrs["inference"] = {"strategy": "model-ensemble"}
    return frame


class SelectionTraceTests(unittest.TestCase):
    def setUp(self):
        self.scout = build_scout({"one": ConstantModel()})

    def test_trace_reproduces_the_live_squad_and_captain(self):
        for seed in range(12):
            predictions = random_predictions(seed)
            live = self.scout.select_optimal_team(predictions)
            team, trace = insights.select_with_trace(predictions)
            self.assertEqual(list(team["id"]), list(live["id"]), seed)
            self.assertEqual(list(team["role"]), list(live["role"]), seed)
            self.assertEqual(list(team["element_type"]), list(live["element_type"]))
            self.assertEqual(sorted(trace["squad_ids"]), sorted(live["id"]))

    def test_club_limit_and_position_reasons(self):
        rows = []
        for index in range(1, 23):
            position = 1 if index <= 3 else 2 if index <= 9 else 3 if index <= 17 else 4
            rows.append(
                {
                    "id": index,
                    "element_type": position,
                    "web_name": f"P{index}",
                    # The four best midfielders share one club.
                    "team_name": "Arsenal" if index in (10, 11, 12, 13) else f"Club {index}",
                    "expected_points": 10.0 - index * 0.1 if index in (10, 11, 12, 13) else 3.0,
                    "availability_factor": 0.0 if index == 14 else 1.0,
                }
            )
        predictions = pd.DataFrame(rows)
        _, trace = insights.select_with_trace(predictions)
        decisions = trace["decisions"]

        self.assertEqual(decisions[13]["status"], "club-limit")
        self.assertEqual(decisions[13]["blocking_club"], "Arsenal")
        self.assertEqual(len(decisions[13]["club_players"]), 3)
        self.assertEqual(decisions[14]["status"], "unavailable")
        self.assertEqual(decisions[3]["status"], "position-full")
        self.assertIn(13, [item["id"] for item in trace["club_limited"]])
        self.assertEqual(trace["club_counts"]["Arsenal"], 3)

    def test_locks_and_excludes_change_only_the_sandbox_squad(self):
        predictions = random_predictions(3)
        live, _ = insights.select_with_trace(predictions)
        captain = int(live.iloc[0]["id"])
        available = predictions.loc[
            (predictions["availability_factor"] > 0)
            & ~predictions["id"].isin(live["id"])
        ]
        outsider = int(available.sort_values("expected_points").iloc[0]["id"])

        team, trace = insights.select_with_trace(
            predictions, locks=[outsider], excludes=[captain]
        )
        self.assertIn(outsider, list(team["id"]))
        self.assertNotIn(captain, list(team["id"]))
        self.assertEqual(trace["decisions"][outsider]["status"], "locked")
        self.assertEqual(trace["decisions"][captain]["status"], "excluded")
        self.assertEqual(len(team), 15)


class ExplanationTests(unittest.TestCase):
    def test_goals_drive_the_explanation_of_a_goals_model(self):
        scout = build_scout({"goals": GoalsModel(), "flat": ConstantModel(2.0)})
        predictions = scout.predict_players(league_history(), gameweek=3)
        inputs = scout.model_inputs(3)
        team, trace = insights.select_with_trace(predictions)
        player_id = int(team.iloc[0]["id"])

        explanation = insights.explain_player(
            scout, inputs, predictions, player_id, trace=trace, team=team
        )

        self.assertFalse(explanation["approximate"])
        self.assertAlmostEqual(
            explanation["expected_points"], explanation["served_points"], places=6
        )
        self.assertEqual(set(explanation["models"]), {"goals", "flat"})
        self.assertTrue(explanation["reasons"])
        self.assertEqual(explanation["role"], "captain")
        self.assertTrue(any("Captain" in reason for reason in explanation["reasons"]))
        # Every player scores one goal, so the typical player equals him.
        self.assertAlmostEqual(explanation["typical_points"], explanation["match_points"])
        steps = explanation["waterfall"]
        self.assertEqual(steps[0]["kind"], "start")
        self.assertAlmostEqual(steps[-1]["value"], explanation["expected_points"], places=3)

    def test_each_models_points_are_clipped_like_the_prediction(self):
        history = league_history()
        history.loc[history["id"] == 3, "goals"] = 4.0
        scout = build_scout({"goals": GoalsModel(), "flat": ConstantModel(2.0)})
        scout.clip_max = 4.0
        predictions = scout.predict_players(history, gameweek=3)
        inputs = scout.model_inputs(3)
        served = insights.model_points_by_id(predictions.attrs["inference"])[3]

        explanation = insights.explain_player(scout, inputs, predictions, 3)

        # The goals model's raw output passes the clip; the explanation, the
        # prediction table and the archive must all show the clipped figure.
        self.assertGreater(float(scout._run_models(inputs.model_input).matrix[0].max()), 4.0)
        for name, values in explanation["models"].items():
            self.assertAlmostEqual(values["expected_points"], served[name], places=2)
        self.assertLessEqual(explanation["models"]["goals"]["match_points"], 4.0 * 2)

    def test_contribution_reflects_a_strong_input(self):
        history = league_history()
        history.loc[history["id"] == 3, "goals"] = 4.0
        scout = build_scout({"goals": GoalsModel()})
        predictions = scout.predict_players(history, gameweek=3)
        inputs = scout.model_inputs(3)

        explanation = insights.explain_player(scout, inputs, predictions, 3)

        top = explanation["contributions"][0]
        self.assertEqual(top["feature"], "goals")
        self.assertAlmostEqual(top["contribution"], 3.0)
        self.assertEqual(top["percentile"], 100.0)
        self.assertEqual(explanation["groups"][0]["group"], "Attack")

    def test_fixture_sensitivity_ranks_home_above_away(self):
        scout = build_scout({"venue": VenueModel()})
        predictions = scout.predict_players(league_history(), gameweek=3)
        inputs = scout.model_inputs(3)
        home_player = int(predictions.loc[predictions["team_name"] == "Club 0", "id"].iloc[0])
        away_player = int(predictions.loc[predictions["team_name"] == "Club 1", "id"].iloc[0])

        home = insights.explain_player(scout, inputs, predictions, home_player)
        away = insights.explain_player(scout, inputs, predictions, away_player)

        self.assertTrue(all(item["home"] for item in home["fixture_sensitivity"][:6]))
        self.assertLess(home["fixture_rank"], away["fixture_rank"])
        self.assertEqual(home["fixtures"][0]["difficulty"], 2)

    def test_unknown_player_raises_key_error(self):
        scout = build_scout({"one": ConstantModel()})
        predictions = scout.predict_players(league_history(), gameweek=3)
        with self.assertRaises(KeyError):
            insights.explain_player(scout, scout.model_inputs(3), predictions, 99999)


class PredictionTableTests(unittest.TestCase):
    def test_rows_carry_models_inputs_and_decisions(self):
        scout = build_scout({"one": ConstantModel(1.0), "two": GoalsModel()})
        predictions = scout.predict_players(league_history(), gameweek=3)
        team, trace = insights.select_with_trace(predictions)
        rows = insights.prediction_table(
            predictions,
            inputs_players=scout.model_inputs(3).players,
            official_players={1: {"price": 5.0, "news": "Knock", "status": "d"}},
            trace=trace,
            team=team,
        )
        self.assertEqual(len(rows), predictions["id"].nunique())
        first = next(row for row in rows if row["id"] == 1)
        self.assertEqual(set(first["models"]), {"one", "two"})
        self.assertAlmostEqual(first["features"]["goals"], 1.0)
        self.assertEqual(first["news"], "Knock")
        self.assertAlmostEqual(first["value"], first["expected_points"] / 5.0, places=3)
        self.assertEqual(sum(row["in_squad"] for row in rows), 15)
        self.assertEqual(sum(row["role"] == "captain" for row in rows), 1)

    def test_model_agreement_summarises_every_model(self):
        scout = build_scout({"one": ConstantModel(1.0), "two": GoalsModel()})
        predictions = scout.predict_players(league_history(), gameweek=3)
        agreement = insights.model_agreement(predictions)
        self.assertTrue(agreement["available"])
        self.assertEqual(agreement["models"], ["one", "two"])
        self.assertEqual(agreement["correlation"]["two"]["two"], None)  # constant input
        self.assertEqual(len(agreement["disagreements"]), 15)


class AccuracyTests(unittest.TestCase):
    def forecast(self):
        frame = pd.DataFrame(
            {
                "id": [1, 2, 3, 4],
                "element_type": [1, 2, 3, 4],
                "web_name": ["A", "B", "C", "D"],
                "team_name": ["X", "Y", "Z", "W"],
                "expected_points": [2.0, 4.0, 6.0, 8.0],
            }
        )
        frame.attrs.update(
            gameweek=5,
            inference={
                "strategy": "model-ensemble",
                "model_predictions": {"ids": [1, 2, 3, 4], "points": {"m": [1, 2, 3, 4]}},
            },
        )
        return frame

    def test_metrics_match_hand_computed_values(self):
        actuals = {
            1: {"points": 1.0, "minutes": 90},
            2: {"points": 5.0, "minutes": 90},
            3: {"points": 6.0, "minutes": 0},
            # Player 4 did not appear in the live payload: zero points.
        }
        result = insights.gameweek_accuracy(self.forecast(), actuals, top_n=2)

        # Errors: +1, -1, 0, +8.
        self.assertEqual(result["n"], 4)
        self.assertAlmostEqual(result["mae"], 2.5)
        self.assertAlmostEqual(result["bias"], 2.0)
        self.assertAlmostEqual(result["rmse"], (66 / 4) ** 0.5, places=4)
        self.assertEqual(result["top_precision"], 0.5)
        self.assertAlmostEqual(result["models"]["m"]["mae"], (0 + 3 + 3 + 4) / 4)

        played = insights.gameweek_accuracy(self.forecast(), actuals, played_only=True)
        self.assertEqual(played["n"], 2)

    def test_summary_pools_every_gameweek_and_calibrates(self):
        first = insights.gameweek_accuracy(self.forecast(), {1: {"points": 2.0, "minutes": 90}})
        second = insights.gameweek_accuracy(self.forecast(), {})
        summary = insights.accuracy_summary([first, second])
        self.assertEqual(summary["gameweeks"], 2)
        self.assertEqual(summary["n"], 8)
        self.assertNotIn("_pairs", first)
        self.assertTrue(summary["calibration"])

    def test_actual_points_read_the_official_live_payload(self):
        payload = {"elements": [{"id": 7, "stats": {"total_points": 9, "minutes": 90}}]}
        self.assertEqual(insights.actual_points(payload), {7: {"points": 9.0, "minutes": 90.0}})


class ForecastDiffTests(unittest.TestCase):
    def test_squad_changes_explain_availability_news(self):
        scout = build_scout({"goals": GoalsModel()})
        before = scout.predict_players(league_history(), gameweek=3)
        captain = int(scout.select_optimal_team(before).iloc[0]["id"])
        after = before.copy()
        after.attrs = dict(before.attrs)
        after.loc[after["id"] == captain, ["availability_factor", "expected_points"]] = 0.0

        diff = insights.forecast_diff(
            before,
            after,
            scout.select_optimal_team,
            current_players={captain: {"news": "Hamstring injury"}},
        )

        out = next(item for item in diff["squad_out"] if item["id"] == captain)
        self.assertIn("availability 100% -> 0%", out["reason"])
        self.assertIn("Hamstring injury", out["reason"])
        self.assertTrue(diff["captain_changed"])
        self.assertEqual(len(diff["squad_in"]), len(diff["squad_out"]))
        self.assertTrue(all(item["reason"] for item in diff["squad_in"]))


if __name__ == "__main__":
    unittest.main()


class ScoutDashboardSupportTests(unittest.TestCase):
    """The scout keeps what the dashboard needs without changing forecasts."""

    def models(self):
        return {"goals": GoalsModel(), "flat": ConstantModel(2.0)}

    def test_per_model_points_average_to_the_ensemble(self):
        scout = build_scout(self.models())
        predictions = scout.predict_players(league_history(), gameweek=3)
        inference = predictions.attrs["inference"]
        points = insights.model_points_by_id(inference)
        for row in predictions.itertuples():
            values = list(points[int(row.id)].values())
            self.assertAlmostEqual(sum(values) / len(values), row.expected_points)
        self.assertIn("goals", inference["feature_coverage"]["populated"])
        self.assertIn("expected_goals", inference["feature_coverage"]["missing"])

    def test_model_inputs_keep_fpl_xp_separate_from_the_projection(self):
        history = league_history()
        history["expected_points"] = 0.5
        scout = build_scout(self.models())
        scout.predict_players(history, gameweek=3)
        inputs = scout.model_inputs(3)
        self.assertEqual(inputs.origin, "prediction")
        self.assertFalse(inputs.approximate)
        self.assertTrue((inputs.players["expected_points"] == 0.5).all())

    def test_overrides_change_weights_and_are_recorded(self):
        scout = build_scout(self.models(), minimum=1)
        baseline_versions = scout._model_versions()
        self.assertNotIn("weight_override", str(baseline_versions))

        scout.set_model_override("goals", weight=3.0)
        scout.set_model_override("flat", enabled=False)
        predictions = scout.predict_players(league_history(), gameweek=3)
        inference = predictions.attrs["inference"]
        self.assertEqual(inference["weights"], {"goals": 3.0})
        self.assertEqual(inference["disabled_models"], ["flat"])
        self.assertEqual(scout._model_versions()["goals"]["weight_override"], 3.0)
        self.assertTrue(scout._model_versions()["flat"]["disabled"])

        with self.assertRaises(ValueError):
            scout.set_model_override("goals", enabled=False)
        with self.assertRaises(ValueError):
            scout.set_model_override("goals", weight=0)
        with self.assertRaises(KeyError):
            scout.set_model_override("missing", weight=1)

        scout.reset_model_overrides()
        self.assertEqual(scout._model_versions(), baseline_versions)

    def test_sandbox_predictions_leave_the_live_state_alone(self):
        scout = build_scout(self.models())
        live = scout.predict_players(league_history(), gameweek=3)
        inputs = scout.model_inputs(3)
        player = int(live.iloc[0]["id"])

        sandbox = scout.predict_from_inputs(
            inputs, weights={"goals": 1.0}, disabled={"flat"}, availability_overrides={player: 0.0}
        )

        self.assertTrue(sandbox.attrs["sandbox"])
        self.assertEqual(sandbox.attrs["inference"]["weights"], {"goals": 1.0})
        self.assertEqual(float(sandbox.loc[sandbox["id"] == player, "expected_points"].iloc[0]), 0.0)
        self.assertEqual(scout.weight_overrides, {})
        self.assertEqual(scout.model_inputs(3), inputs)
        self.assertTrue((inputs.players["availability_factor"] == 1.0).all())

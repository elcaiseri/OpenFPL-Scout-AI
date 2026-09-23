import argparse
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from scripts.export_model_inputs import main, parse_gameweeks
from src.features import MODEL_FEATURES


class FakeClient:
    def next_gameweek(self):
        return 4


class FakeScout:
    official_client = FakeClient()

    def __init__(self):
        self.gameweeks = []

    def export_model_inputs(self, gameweek):
        self.gameweeks.append(gameweek)
        if gameweek == 1:
            return pd.DataFrame(columns=["id", *MODEL_FEATURES]), {
                "gameweek": 1,
                "strategy": "ownership-cold-start",
                "rows": 0,
                "players": 0,
                "data_enrichment": {"status": "before-start-gameweek"},
                "feature_sources": {},
            }
        frame = pd.DataFrame([{"id": 7, **{feature: 1.0 for feature in MODEL_FEATURES}}])
        frame["total_shots"] = float("nan")
        return frame, {
            "gameweek": gameweek,
            "strategy": "model-ensemble",
            "rows": 1,
            "players": 1,
            "data_enrichment": {"status": "applied", "unenriched_gameweeks": []},
            "feature_sources": {
                feature: "fpl-data" if feature == "total_shots" else "official-fpl"
                for feature in MODEL_FEATURES
            },
        }


class ExportModelInputsTests(unittest.TestCase):
    def test_parses_gameweek_ranges_and_lists(self):
        self.assertEqual(parse_gameweeks("1-7"), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(parse_gameweeks("5,2-3"), [2, 3, 5])
        for invalid in ("0-2", "37-39", "x", ""):
            with self.subTest(value=invalid), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                parse_gameweeks(invalid)

    def test_writes_inputs_sources_and_summary_per_gameweek(self):
        scout = FakeScout()
        with TemporaryDirectory() as directory, patch("builtins.print"):
            output = Path(directory)
            main(
                ["--gameweeks", "1-2", "--output", str(output)],
                scout_factory=lambda config: scout,
            )

            self.assertEqual(scout.gameweeks, [1, 2])
            self.assertTrue(pd.read_csv(output / "gw_01.csv").empty)
            gw2 = pd.read_csv(output / "gw_02.csv")
            self.assertEqual(list(gw2.columns), ["id", *MODEL_FEATURES])
            sources = pd.read_csv(output / "feature_sources.csv")
            shots = sources.loc[sources["feature"] == "total_shots"].iloc[0]
            self.assertEqual(shots["source"], "fpl-data")
            self.assertEqual(shots["coverage"], 0.0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(
                [item["strategy"] for item in summary],
                ["ownership-cold-start", "model-ensemble"],
            )

    def test_a_failed_gameweek_is_reported_without_stopping_the_rest(self):
        scout = FakeScout()
        original = scout.export_model_inputs

        def export(gameweek):
            if gameweek == 3:
                raise RuntimeError("Official FPL returned HTTP 503")
            return original(gameweek)

        scout.export_model_inputs = export
        with TemporaryDirectory() as directory, patch("builtins.print"):
            output = Path(directory)
            (output / "gw_03.csv").write_text("stale")
            status = main(
                ["--gameweeks", "2-4", "--output", str(output)],
                scout_factory=lambda config: scout,
            )

            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(status, 1)
            self.assertEqual(scout.gameweeks, [2, 4])
            self.assertFalse((output / "gw_03.csv").exists())
            self.assertTrue((output / "gw_04.csv").is_file())
            self.assertEqual(
                summary[1], {"gameweek": 3, "error": "Official FPL returned HTTP 503"}
            )

    def test_defaults_to_the_next_gameweek(self):
        scout = FakeScout()
        with TemporaryDirectory() as directory, patch("builtins.print"):
            main(["--output", directory], scout_factory=lambda config: scout)

        self.assertEqual(scout.gameweeks, [4])


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from scripts.download_fpl_data import (
    FPLDataClient,
    Season,
    _atomic_write,
    _resolve_season,
    check_for_regression,
    validate_csv,
)


HEADER = (
    "id,element_type,web_name,team_name,opponent_team_name,was_home,"
    "gameweek,minutes,total_points,shots,npxG,npG,key_passes,xCS,npxGI,xP,"
    "PvsxP,touches,penalty_area_touches\n"
)


def csv_bytes(rows=100, observed_gameweek=2):
    body = []
    for index in range(rows):
        player_id = index % 60 + 1
        gameweek = index % 4 + 1
        minutes = 90 if gameweek <= observed_gameweek else 0
        body.append(
            f"{player_id},{player_id % 4 + 1},Player {player_id},Arsenal,"
            f"Chelsea,True,{gameweek},{minutes},2,1,0.2,0,1,0.3,0.4,3,2,30,4\n"
        )
    return (HEADER + "".join(body)).encode()


class FPLDataDownloadTests(unittest.TestCase):
    def test_discovers_seasons_from_statistics_page(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "response": {
                "page-content": {
                    "children": {
                        "props": {
                            "id": "input-year",
                            "options": [
                                {"label": "2025/26", "value": "2025_26"},
                                {"label": "2024/25", "value": "2024_25"},
                            ],
                        }
                    }
                }
            }
        }
        session = Mock()
        session.headers = {}
        session.post.return_value = response

        seasons = FPLDataClient(session=session).available_seasons()

        self.assertEqual([season.end_year for season in seasons], [2026, 2025])
        request = session.post.call_args.kwargs["json"]
        self.assertEqual(request["inputs"][0]["value"], "/statistics")

    def test_decodes_public_download_callback(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "response": {
                "download-dataframe-csv": {
                    "data": {
                        "content": csv_bytes().decode(),
                        "filename": "fpl-data-stats.csv",
                        "base64": False,
                    }
                }
            }
        }
        session = Mock()
        session.headers = {}
        session.post.return_value = response
        season = Season("2025/26", "2025_26", 2025, 2026)

        raw, filename = FPLDataClient(session=session).download_csv(season)

        self.assertEqual(raw, csv_bytes())
        self.assertEqual(filename, "fpl-data-stats.csv")
        request = session.post.call_args.kwargs["json"]
        self.assertEqual(request["state"][0]["value"], "2025_26")

    def test_validates_and_reports_canonical_feature_coverage(self):
        summary = validate_csv(csv_bytes())

        self.assertEqual(summary.rows, 100)
        self.assertEqual(summary.players, 60)
        self.assertEqual(summary.latest_observed_gameweek, 2)
        self.assertIn("total_shots", summary.supplied_target_features)
        self.assertIn("touches_opp_box", summary.supplied_target_features)
        self.assertIn("clearances", summary.missing_target_features)

    def test_rejects_non_dataset_content(self):
        with self.assertRaisesRegex(ValueError, "missing required columns"):
            validate_csv(b"html,error\n<body>,upstream failure\n", minimum_rows=1)

    def test_accepts_historical_manager_rows_without_counting_them_as_players(self):
        raw = (
            csv_bytes()
            + ("999,5,Arteta,Arsenal,Chelsea,True,2,0,8,0,0,0,0,0,0,0,0,0,0\n").encode()
        )

        summary = validate_csv(raw)

        self.assertEqual(summary.rows, 101)
        self.assertEqual(summary.players, 60)

    def test_rejects_gameweek_and_feature_regressions(self):
        current = validate_csv(csv_bytes(observed_gameweek=3))
        incoming = validate_csv(csv_bytes(observed_gameweek=2))

        with self.assertRaisesRegex(ValueError, "regresses observed gameweek"):
            check_for_regression(current, incoming)

    def test_resolves_latest_value_and_end_year(self):
        seasons = [
            Season("2024/25", "2024_25", 2024, 2025),
            Season("2025/26", "2025_26", 2025, 2026),
        ]

        self.assertEqual(_resolve_season("latest", seasons).value, "2025_26")
        self.assertEqual(_resolve_season("2026", seasons).value, "2025_26")
        self.assertEqual(_resolve_season("2024/25", seasons).value, "2024_25")

    def test_atomic_write_replaces_complete_file(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "dataset.csv"
            destination.write_bytes(b"old")

            _atomic_write(destination, b"new")

            self.assertEqual(destination.read_bytes(), b"new")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

import unittest

import pandas as pd

from scripts.download_fpl_data import Season
from src.fpl_data_inference import FPLDataHistoryProvider


def source_csv():
    header = (
        "id,element_type,web_name,team_name,opponent_team_name,was_home,"
        "gameweek,minutes,total_points,total_shots,clearances\n"
    )
    rows = []
    for player_id in range(1, 61):
        for gameweek in (1, 2):
            rows.append(
                f"{player_id},{player_id % 4 + 1},Player {player_id},Arsenal,"
                f"Chelsea,True,{gameweek},90,2,{player_id + gameweek},3\n"
            )
    return (header + "".join(rows)).encode()


def official_history():
    return pd.DataFrame(
        [
            {
                "id": 1,
                "element_type": 2,
                "web_name": "Player 1",
                "team_name": "Arsenal",
                "opponent_team_name": "Chelsea",
                "was_home": True,
                "gameweek": 1,
                "minutes": 90,
                "total_points": 2,
                "total_shots": 99,
            },
            {
                "id": 1,
                "element_type": 2,
                "web_name": "Player 1",
                "team_name": "Arsenal",
                "opponent_team_name": "Chelsea",
                "was_home": True,
                "gameweek": 2,
                "minutes": 90,
                "total_points": 2,
            },
        ]
    )


class FakeClient:
    def __init__(self, seasons=None):
        self.seasons = seasons or [Season("2026/27", "2026_27", 2026, 2027)]
        self.available_calls = 0
        self.download_calls = 0

    def available_seasons(self):
        self.available_calls += 1
        return self.seasons

    def download_csv(self, season):
        self.download_calls += 1
        return source_csv(), "fpl-data-stats.csv"


class FPLDataHistoryProviderTests(unittest.TestCase):
    def test_enriches_exact_match_without_overriding_official_values(self):
        client = FakeClient()
        provider = FPLDataHistoryProvider(
            "2026_27",
            client=client,
            minimum_match_ratio=1.0,
            refresh_ttl_seconds=3600,
        )

        result, diagnostics = provider.enrich(official_history(), target_gameweek=3)

        self.assertEqual(diagnostics["status"], "applied")
        self.assertEqual(diagnostics["matched_rows"], 2)
        self.assertEqual(result.loc[0, "total_shots"], 99)
        self.assertEqual(result.loc[1, "total_shots"], 3)
        self.assertEqual(result.loc[0, "clearances"], 3)
        self.assertEqual(client.download_calls, 1)

    def test_rejects_wrong_season_and_negative_caches_failure(self):
        client = FakeClient(seasons=[Season("2025/26", "2025_26", 2025, 2026)])
        provider = FPLDataHistoryProvider(
            "2026_27", client=client, refresh_ttl_seconds=3600
        )

        first, first_diagnostics = provider.enrich(
            official_history(), target_gameweek=3
        )
        second, second_diagnostics = provider.enrich(
            official_history(), target_gameweek=3
        )

        self.assertEqual(first_diagnostics["status"], "unavailable")
        self.assertEqual(second_diagnostics["status"], "unavailable")
        self.assertTrue(first.equals(official_history()))
        self.assertTrue(second.equals(official_history()))
        self.assertEqual(client.available_calls, 1)
        self.assertEqual(client.download_calls, 0)

    def test_rejects_source_that_is_behind_official_history(self):
        raw = source_csv().replace(b",2,90,2,", b",2,0,2,")
        client = FakeClient()
        client.download_csv = lambda season: (raw, "fpl-data-stats.csv")
        provider = FPLDataHistoryProvider(
            "2026_27", client=client, refresh_ttl_seconds=3600
        )

        result, diagnostics = provider.enrich(official_history(), target_gameweek=3)

        self.assertEqual(diagnostics["status"], "stale")
        self.assertNotIn("clearances", result.columns)

    def test_rejects_low_match_ratio_instead_of_partially_merging(self):
        history = official_history()
        history.loc[1, "opponent_team_name"] = "Liverpool"
        provider = FPLDataHistoryProvider(
            "2026_27",
            client=FakeClient(),
            minimum_match_ratio=0.8,
            refresh_ttl_seconds=3600,
        )

        result, diagnostics = provider.enrich(history, target_gameweek=3)

        self.assertEqual(diagnostics["status"], "rejected-low-match-ratio")
        self.assertEqual(diagnostics["match_ratio"], 0.5)
        self.assertNotIn("clearances", result.columns)

    def test_double_gameweek_rows_match_by_opponent_and_home_away(self):
        raw = (
            source_csv()
            + ("1,2,Player 1,Arsenal,Liverpool,False,2,90,5,8,4\n").encode()
        )
        client = FakeClient()
        client.download_csv = lambda season: (raw, "fpl-data-stats.csv")
        history = pd.concat(
            [
                official_history(),
                pd.DataFrame(
                    [
                        {
                            "id": 1,
                            "element_type": 2,
                            "web_name": "Player 1",
                            "team_name": "Arsenal",
                            "opponent_team_name": "Liverpool",
                            "was_home": False,
                            "gameweek": 2,
                            "minutes": 90,
                            "total_points": 5,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
        provider = FPLDataHistoryProvider(
            "2026_27",
            client=client,
            minimum_match_ratio=1.0,
            refresh_ttl_seconds=3600,
        )

        result, diagnostics = provider.enrich(history, target_gameweek=3)

        self.assertEqual(diagnostics["status"], "applied")
        self.assertEqual(diagnostics["matched_rows"], 3)
        self.assertEqual(result.loc[2, "total_shots"], 8)


if __name__ == "__main__":
    unittest.main()

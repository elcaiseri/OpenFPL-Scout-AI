import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.download_fpl_data import Season
from test_download_fpl_data import fail_replacing
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


def grown_csv(extra_players):
    """The source CSV plus ``extra_players`` more GW1 rows (newer coverage)."""
    rows = "".join(
        f"{900 + index},2,Extra {index},Arsenal,Chelsea,True,1,90,2,1,3\n"
        for index in range(extra_players)
    )
    return source_csv() + rows.encode()


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
            acknowledge_permission_pending=True,
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
            "2026_27",
            client=client,
            refresh_ttl_seconds=3600,
            acknowledge_permission_pending=True,
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
            "2026_27",
            client=client,
            refresh_ttl_seconds=3600,
            max_gameweek_lag=0,
            acknowledge_permission_pending=True,
        )

        result, diagnostics = provider.enrich(official_history(), target_gameweek=3)

        self.assertEqual(diagnostics["status"], "stale")
        self.assertNotIn("clearances", result.columns)

    def test_one_gameweek_lag_enriches_covered_rows_only(self):
        # FPL Data has not published GW2 yet (its GW2 rows are unplayed
        # placeholders), as during a live gameweek or just after it.
        raw = source_csv().replace(b",2,90,2,", b",2,0,2,")
        client = FakeClient()
        client.download_csv = lambda season: (raw, "fpl-data-stats.csv")
        provider = FPLDataHistoryProvider(
            "2026_27",
            client=client,
            minimum_match_ratio=1.0,
            refresh_ttl_seconds=3600,
            acknowledge_permission_pending=True,
        )

        result, diagnostics = provider.enrich(official_history(), target_gameweek=3)

        self.assertEqual(diagnostics["status"], "applied")
        self.assertEqual(diagnostics["unenriched_gameweeks"], [2])
        self.assertEqual(diagnostics["covered_rows"], 1)
        self.assertEqual(result.loc[0, "clearances"], 3)
        self.assertTrue(pd.isna(result.loc[1, "clearances"]))
        self.assertTrue(pd.isna(result.loc[1, "total_shots"]))

    def test_rejects_source_beyond_the_allowed_lag(self):
        raw = source_csv().replace(b",2,90,2,", b",2,0,2,")
        client = FakeClient()
        client.download_csv = lambda season: (raw, "fpl-data-stats.csv")
        history = pd.concat(
            [official_history(), official_history().iloc[[1]].assign(gameweek=3)],
            ignore_index=True,
        )
        provider = FPLDataHistoryProvider(
            "2026_27",
            client=client,
            refresh_ttl_seconds=3600,
            acknowledge_permission_pending=True,
        )

        result, diagnostics = provider.enrich(history, target_gameweek=4)

        self.assertEqual(diagnostics["status"], "stale")
        self.assertIn("at most 1 gameweek", diagnostics["error"])
        self.assertNotIn("clearances", result.columns)

    def test_rejects_low_match_ratio_instead_of_partially_merging(self):
        history = official_history()
        history.loc[1, "opponent_team_name"] = "Liverpool"
        provider = FPLDataHistoryProvider(
            "2026_27",
            client=FakeClient(),
            minimum_match_ratio=0.8,
            refresh_ttl_seconds=3600,
            acknowledge_permission_pending=True,
        )

        result, diagnostics = provider.enrich(history, target_gameweek=3)

        self.assertEqual(diagnostics["status"], "rejected-low-match-ratio")
        self.assertEqual(diagnostics["match_ratio"], 0.5)
        self.assertEqual(diagnostics["unmatched_opponents"], {"liverpool": 1})
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
            acknowledge_permission_pending=True,
        )

        result, diagnostics = provider.enrich(history, target_gameweek=3)

        self.assertEqual(diagnostics["status"], "applied")
        self.assertEqual(diagnostics["matched_rows"], 3)
        self.assertEqual(result.loc[2, "total_shots"], 8)

    def test_pending_permission_without_acknowledgement_never_downloads(self):
        client = FakeClient()
        provider = FPLDataHistoryProvider(
            "2026_27", client=client, refresh_ttl_seconds=3600
        )

        result, diagnostics = provider.enrich(official_history(), target_gameweek=3)

        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertFalse(diagnostics["remote_download_allowed"])
        self.assertIn("not acknowledged", diagnostics["error"])
        self.assertEqual(client.available_calls, 0)
        self.assertEqual(client.download_calls, 0)
        self.assertTrue(result.equals(official_history()))

    def test_granted_permission_allows_downloads(self):
        provider = FPLDataHistoryProvider(
            "2026_27", client=FakeClient(), permission_status="granted"
        )

        self.assertTrue(provider.remote_download_allowed)

    def test_refresh_does_not_block_requests_that_have_a_dataset(self):
        now = [0.0]
        client = FakeClient()
        provider = FPLDataHistoryProvider(
            "2026_27",
            client=client,
            refresh_ttl_seconds=60,
            minimum_match_ratio=1.0,
            acknowledge_permission_pending=True,
            clock=lambda: now[0],
        )
        provider.enrich(official_history(), target_gameweek=3)

        started = threading.Event()
        release = threading.Event()

        def slow_download(season):
            started.set()
            release.wait(5)
            return source_csv(), "fpl-data-stats.csv"

        client.download_csv = slow_download
        now[0] = 61
        refresher = threading.Thread(
            target=provider.enrich, args=(official_history(), 3)
        )
        refresher.start()
        self.assertTrue(started.wait(5))

        _, diagnostics = provider.enrich(official_history(), target_gameweek=3)
        release.set()
        refresher.join(5)

        self.assertEqual(diagnostics["status"], "applied")
        self.assertEqual(diagnostics["cache"], "stale-memory-refreshing")
        self.assertFalse(refresher.is_alive())

    def test_regressed_download_never_replaces_the_dataset_in_use(self):
        now = [0.0]
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "fpl-data.csv"
            provider = FPLDataHistoryProvider(
                "2026_27",
                client=client,
                runtime_cache_path=cache,
                minimum_match_ratio=1.0,
                refresh_ttl_seconds=60,
                acknowledge_permission_pending=True,
                clock=lambda: now[0],
            )
            _, first = provider.enrich(official_history(), target_gameweek=3)
            cached_bytes = cache.read_bytes()

            # The source drops the clearances column: a coverage regression.
            regressed = b"".join(
                line.rsplit(b",", 1)[0] + b"\n"
                for line in source_csv().splitlines()
            )
            client.download_csv = lambda season: (regressed, "fpl-data-stats.csv")
            now[0] = 61
            result, second = provider.enrich(official_history(), target_gameweek=3)

            self.assertEqual(second["status"], "applied")
            self.assertEqual(second["dataset_sha256"], first["dataset_sha256"])
            self.assertIn("loses model feature columns", second["refresh_error"])
            self.assertEqual(result.loc[0, "clearances"], 3)
            self.assertEqual(cache.read_bytes(), cached_bytes)

    def test_unchanged_download_does_not_rewrite_the_cache(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "fpl-data.csv"
            provider = FPLDataHistoryProvider(
                "2026_27",
                client=FakeClient(),
                runtime_cache_path=cache,
                refresh_ttl_seconds=60,
                acknowledge_permission_pending=True,
                clock=lambda: now[0],
            )
            provider.enrich(official_history(), target_gameweek=3)
            metadata = json.loads(
                cache.with_suffix(".metadata.json").read_text(encoding="utf-8")
            )

            now[0] = 61
            with patch("src.fpl_data_inference._atomic_write") as write, patch(
                "src.fpl_data_inference.atomic_write_pair"
            ) as write_pair:
                _, diagnostics = provider.enrich(official_history(), target_gameweek=3)

            self.assertEqual(diagnostics["cache"], "remote")
            write.assert_not_called()
            write_pair.assert_not_called()
            self.assertEqual(metadata["source_filename"], "fpl-data-stats.csv")
            self.assertEqual(metadata["rows"], 120)

    def test_failed_metadata_commit_keeps_the_cached_pair_consistent(self):
        now = [0.0]
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "fpl-data.csv"
            provider = FPLDataHistoryProvider(
                "2026_27",
                client=client,
                runtime_cache_path=cache,
                refresh_ttl_seconds=60,
                acknowledge_permission_pending=True,
                clock=lambda: now[0],
            )
            provider.enrich(official_history(), target_gameweek=3)
            original = cache.read_bytes()

            client.download_csv = lambda season: (grown_csv(5), "fpl-data-stats.csv")
            now[0] = 61
            with fail_replacing(cache.with_suffix(".metadata.json").name):
                provider.enrich(official_history(), target_gameweek=3)

            self.assertEqual(cache.read_bytes(), original)
            provider._read_local(cache)  # Checksum still matches its metadata.

    def test_smaller_download_never_replaces_the_dataset_in_use(self):
        now = [0.0]
        client = FakeClient()
        client.download_csv = lambda season: (grown_csv(10), "fpl-data-stats.csv")
        provider = FPLDataHistoryProvider(
            "2026_27",
            client=client,
            refresh_ttl_seconds=60,
            acknowledge_permission_pending=True,
            clock=lambda: now[0],
        )
        _, first = provider.enrich(official_history(), target_gameweek=3)

        # 130 rows and 70 players shrink to 120 and 60: inside the importer's
        # 20% churn allowance, but never acceptable for an unattended refresh.
        client.download_csv = lambda season: (source_csv(), "fpl-data-stats.csv")
        now[0] = 61
        _, second = provider.enrich(official_history(), target_gameweek=3)

        self.assertEqual(second["dataset_sha256"], first["dataset_sha256"])
        self.assertIn("row count dropped", second["refresh_error"])

    def test_failed_refresh_keeps_a_newer_dataset_than_the_local_copy(self):
        now = [0.0]
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "fpl-data.csv"
            provider = FPLDataHistoryProvider(
                "2026_27",
                client=client,
                runtime_cache_path=cache,
                refresh_ttl_seconds=60,
                acknowledge_permission_pending=True,
                clock=lambda: now[0],
            )
            provider.enrich(official_history(), target_gameweek=3)

            # A newer download that cannot be persisted, then an outage.
            client.download_csv = lambda season: (grown_csv(5), "fpl-data-stats.csv")
            now[0] = 61
            with patch(
                "src.fpl_data_inference.atomic_write_pair",
                side_effect=OSError("read-only"),
            ):
                _, newer = provider.enrich(official_history(), target_gameweek=3)

            def outage(season):
                raise RuntimeError("FPL Data is down")

            client.download_csv = outage
            now[0] = 122
            _, after = provider.enrich(official_history(), target_gameweek=3)

            self.assertEqual(after["dataset_sha256"], newer["dataset_sha256"])
            self.assertEqual(after["cache"], "stale-memory-fallback")
            self.assertIn("FPL Data is down", after["refresh_error"])

    def test_identical_download_upgrades_old_metadata_without_rewriting_data(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "fpl-data.csv"
            provider = FPLDataHistoryProvider(
                "2026_27",
                client=FakeClient(),
                runtime_cache_path=cache,
                refresh_ttl_seconds=60,
                acknowledge_permission_pending=True,
                clock=lambda: now[0],
            )
            provider.enrich(official_history(), target_gameweek=3)
            metadata_file = cache.with_suffix(".metadata.json")
            current = json.loads(metadata_file.read_text(encoding="utf-8"))
            legacy_keys = ("cached_at_utc", "license_status", "season", "sha256")
            metadata_file.write_text(
                json.dumps({key: current[key] for key in legacy_keys}),
                encoding="utf-8",
            )

            now[0] = 61
            with patch("src.fpl_data_inference.atomic_write_pair") as write_pair:
                provider.enrich(official_history(), target_gameweek=3)

            write_pair.assert_not_called()
            upgraded = json.loads(metadata_file.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["source_filename"], "fpl-data-stats.csv")
            self.assertEqual(upgraded["sha256"], current["sha256"])

    def test_identical_download_with_a_new_filename_refreshes_provenance(self):
        now = [0.0]
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "fpl-data.csv"
            provider = FPLDataHistoryProvider(
                "2026_27",
                client=client,
                runtime_cache_path=cache,
                refresh_ttl_seconds=60,
                acknowledge_permission_pending=True,
                clock=lambda: now[0],
            )
            provider.enrich(official_history(), target_gameweek=3)

            client.download_csv = lambda season: (source_csv(), "renamed.csv")
            now[0] = 61
            with patch("src.fpl_data_inference.atomic_write_pair") as write_pair:
                provider.enrich(official_history(), target_gameweek=3)

            write_pair.assert_not_called()
            metadata = json.loads(
                cache.with_suffix(".metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["source_filename"], "renamed.csv")


if __name__ == "__main__":
    unittest.main()

"""Client and schema adapters for the official Fantasy Premier League API."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.features import TEAM_NAME_ALIASES
from src.logger import get_logger

logger = get_logger(__name__)

OFFICIAL_FPL_API = "https://fantasy.premierleague.com/api"


class OfficialFPLAPIError(RuntimeError):
    """Raised when official FPL data cannot be fetched or validated."""


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


def _team_name(name: Any) -> Any:
    if pd.isna(name):
        return np.nan
    return TEAM_NAME_ALIASES.get(str(name), str(name))


class OfficialFPLClient:
    """Read players, match history, gameweeks, and fixtures from FPL."""

    def __init__(
        self,
        base_url: str = OFFICIAL_FPL_API,
        timeout: float = 20.0,
        cache_ttl: int = 300,
        history_cache_ttl: int = 900,
        max_workers: int = 8,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.history_cache_ttl = history_cache_ttl
        self.max_workers = max(1, max_workers)
        self.session = session or self._build_session()
        self._cache: Dict[str, _CacheEntry] = {}
        self._cache_lock = RLock()

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "OpenFPL-Scout-AI/5.0 (+official FPL data)",
            }
        )
        return session

    def _get_json(self, path: str, ttl: Optional[int] = None) -> Any:
        cache_key = path.lstrip("/")
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at > now:
                return cached.value

        url = f"{self.base_url}/{cache_key}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            value = response.json()
        except (requests.RequestException, ValueError) as error:
            raise OfficialFPLAPIError(f"Official FPL request failed: {url}") from error

        with self._cache_lock:
            self._cache[cache_key] = _CacheEntry(
                value=value,
                expires_at=now + (self.cache_ttl if ttl is None else ttl),
            )
        return value

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def bootstrap(self) -> Mapping[str, Any]:
        payload = self._get_json("bootstrap-static/")
        required = {"elements", "teams", "events", "element_types"}
        if not isinstance(payload, Mapping) or not required.issubset(payload):
            raise OfficialFPLAPIError("Official bootstrap response has an invalid schema")
        return payload

    def fixtures(self) -> List[Mapping[str, Any]]:
        payload = self._get_json("fixtures/")
        if not isinstance(payload, list):
            raise OfficialFPLAPIError("Official fixtures response has an invalid schema")
        return payload

    def available_gameweeks(self) -> Dict[str, Any]:
        """Return completed gameweeks plus the current/next playable event."""
        events = self.bootstrap()["events"]
        available = [
            int(event["id"])
            for event in events
            if event.get("finished") or event.get("is_current") or event.get("is_next")
        ]
        if not available and events:
            available = [int(events[0]["id"])]
        available = sorted(set(available))
        return {
            "gameweeks": available,
            "total": len(available),
            "latest": max(available) if available else None,
            "source": "official-fpl",
        }

    def next_gameweek(self) -> int:
        events = self.bootstrap()["events"]
        for state in ("is_next", "is_current"):
            match = next((event for event in events if event.get(state)), None)
            if match:
                return int(match["id"])
        finished = [int(event["id"]) for event in events if event.get("finished")]
        return min(max(finished, default=0) + 1, 38) or 1

    def fixtures_for_gameweek(
        self, gameweek: int, _unused_mapping: Optional[Mapping[str, str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Map official team names to their event fixture context."""
        bootstrap = self.bootstrap()
        team_names = {
            int(team["id"]): _team_name(team["name"])
            for team in bootstrap["teams"]
        }
        matches = sorted(
            (fixture for fixture in self.fixtures() if fixture.get("event") == gameweek),
            key=lambda fixture: (fixture.get("kickoff_time") or "", fixture.get("id", 0)),
        )

        by_team: Dict[str, List[Dict[str, Any]]] = {}
        for fixture in matches:
            home = team_names.get(int(fixture["team_h"]))
            away = team_names.get(int(fixture["team_a"]))
            if not home or not away:
                continue
            by_team.setdefault(home, []).append(
                self._fixture_record(fixture, away, was_home=True)
            )
            by_team.setdefault(away, []).append(
                self._fixture_record(fixture, home, was_home=False)
            )

        result: Dict[str, Dict[str, Any]] = {}
        for team, team_matches in by_team.items():
            primary = dict(team_matches[0])
            primary["fixture_count"] = len(team_matches)
            primary["fixtures"] = team_matches
            if len(team_matches) > 1:
                primary["opponent_team_name"] = " / ".join(
                    str(match["opponent_team_name"]) for match in team_matches
                )
            result[team] = primary
        return result

    @staticmethod
    def _fixture_record(
        fixture: Mapping[str, Any], opponent: str, was_home: bool
    ) -> Dict[str, Any]:
        return {
            "fixture_id": int(fixture["id"]),
            "opponent_team_name": opponent,
            "was_home": was_home,
            "kickoff_time": fixture.get("kickoff_time"),
            "finished": bool(fixture.get("finished")),
            "difficulty": fixture.get(
                "team_h_difficulty" if was_home else "team_a_difficulty"
            ),
        }

    def player_history(
        self, gameweek: Optional[int] = None, selectable_only: bool = True
    ) -> pd.DataFrame:
        """Build model-compatible history solely from official FPL responses."""
        bootstrap = self.bootstrap()
        target_gameweek = int(gameweek or self.next_gameweek())
        players = list(bootstrap["elements"])
        if selectable_only:
            players = [
                player
                for player in players
                if player.get("can_select", not player.get("removed", False))
            ]
        teams = {
            int(team["id"]): _team_name(team["name"])
            for team in bootstrap["teams"]
        }

        has_prior_event = any(
            event.get("finished") and int(event["id"]) < target_gameweek
            for event in bootstrap["events"]
        )
        if not has_prior_event:
            return self._bootstrap_baseline(players, teams)

        summaries = self._fetch_player_summaries(players)
        rows: List[Dict[str, Any]] = []
        players_with_history = set()
        for player in players:
            summary = summaries.get(int(player["id"]), {})
            for history in summary.get("history", []):
                if int(history.get("round", 0)) >= target_gameweek:
                    continue
                rows.append(self._history_row(player, history, teams))
                players_with_history.add(int(player["id"]))

        if not rows:
            logger.warning(
                "Official per-player histories are empty; using bootstrap baseline"
            )
            return self._bootstrap_baseline(players, teams)

        missing_players = [
            player
            for player in players
            if int(player["id"]) not in players_with_history
        ]
        if missing_players:
            logger.info(
                "Using official bootstrap identity baselines for %d players without "
                "prior match history",
                len(missing_players),
            )
            baseline = self._bootstrap_baseline(missing_players, teams)
            rows.extend(baseline.to_dict(orient="records"))
        return pd.DataFrame(rows)

    def _fetch_player_summaries(
        self, players: List[Mapping[str, Any]]
    ) -> Dict[int, Mapping[str, Any]]:
        summaries: Dict[int, Mapping[str, Any]] = {}

        def fetch(player_id: int) -> Tuple[int, Mapping[str, Any]]:
            payload = self._get_json(
                f"element-summary/{player_id}/", ttl=self.history_cache_ttl
            )
            if not isinstance(payload, Mapping) or "history" not in payload:
                raise OfficialFPLAPIError(
                    f"Official summary for player {player_id} has an invalid schema"
                )
            return player_id, payload

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            pending = {
                executor.submit(fetch, int(player["id"])): int(player["id"])
                for player in players
            }
            for future in as_completed(pending):
                player_id = pending[future]
                try:
                    key, payload = future.result()
                    summaries[key] = payload
                except OfficialFPLAPIError as error:
                    logger.warning("Skipping official history for player %d: %s", player_id, error)

        if not summaries:
            raise OfficialFPLAPIError("No official player histories could be fetched")
        return summaries

    @staticmethod
    def _bootstrap_baseline(
        players: List[Mapping[str, Any]], teams: Mapping[int, str]
    ) -> pd.DataFrame:
        # Before GW1, bootstrap statistics are current-season zero totals—not a
        # played match. Keep history unknown so the fitted pipeline applies the
        # same fold-trained imputers used for first-gameweek training rows.
        rows = []
        for player in players:
            rows.append(
                OfficialFPLClient._common_player_values(player, teams)
                | {
                    "gameweek": 0,
                    "was_home": np.nan,
                    "opponent_team_name": np.nan,
                    "now_cost": np.nan,
                    "selected_by_percent": np.nan,
                    "total_points": np.nan,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _history_row(
        player: Mapping[str, Any],
        history: Mapping[str, Any],
        teams: Mapping[int, str],
    ) -> Dict[str, Any]:
        return (
            OfficialFPLClient._common_player_values(player, teams)
            | {
                "gameweek": int(history["round"]),
                "was_home": bool(history.get("was_home")),
                "opponent_team_name": teams.get(int(history["opponent_team"])),
                "now_cost": OfficialFPLClient._cost(history.get("value")),
                "selected_by_percent": OfficialFPLClient._number(
                    player.get("selected_by_percent")
                ),
                "total_points": OfficialFPLClient._number(
                    history.get("total_points")
                ),
                **OfficialFPLClient._official_stats(history),
            }
        )

    @staticmethod
    def _common_player_values(
        player: Mapping[str, Any], teams: Mapping[int, str]
    ) -> Dict[str, Any]:
        return {
            "id": int(player["id"]),
            "element_type": int(player["element_type"]),
            "web_name": player.get("web_name") or player.get("second_name"),
            "team_name": teams.get(int(player["team"])),
        }

    @staticmethod
    def _official_stats(values: Mapping[str, Any]) -> Dict[str, Any]:
        clean_sheets = OfficialFPLClient._number(values.get("clean_sheets"))
        return {
            "minutes": OfficialFPLClient._number(values.get("minutes")),
            "goals": OfficialFPLClient._number(values.get("goals_scored")),
            "assists": OfficialFPLClient._number(values.get("assists")),
            "expected_goals": OfficialFPLClient._number(values.get("expected_goals")),
            "expected_assists": OfficialFPLClient._number(
                values.get("expected_assists")
            ),
            "expected_goal_involvements": OfficialFPLClient._number(
                values.get("expected_goal_involvements")
            ),
            "expected_goals_conceded": OfficialFPLClient._number(
                values.get("expected_goals_conceded")
            ),
            "goals_conceded": OfficialFPLClient._number(
                values.get("goals_conceded")
            ),
            "clean_sheet": clean_sheets,
            "clearances_blocks_interceptions": OfficialFPLClient._number(
                values.get("clearances_blocks_interceptions")
            ),
            "recoveries": OfficialFPLClient._number(values.get("recoveries")),
            "tackles": OfficialFPLClient._number(values.get("tackles")),
            "defensive_contribution": OfficialFPLClient._number(
                values.get("defensive_contribution")
            ),
        }

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return np.nan

    @staticmethod
    def _cost(value: Any) -> float:
        numeric = OfficialFPLClient._number(value)
        return numeric / 10.0 if np.isfinite(numeric) else np.nan

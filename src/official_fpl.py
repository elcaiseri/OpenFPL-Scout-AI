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


class OfficialFPLNotFoundError(OfficialFPLAPIError):
    """Raised when an official FPL resource does not exist."""


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
            raise OfficialFPLAPIError(
                "Official bootstrap response has an invalid schema"
            )
        return payload

    def fixtures(self) -> List[Mapping[str, Any]]:
        payload = self._get_json("fixtures/")
        if not isinstance(payload, list):
            raise OfficialFPLAPIError(
                "Official fixtures response has an invalid schema"
            )
        return payload

    def player_summary(self, player_id: int) -> Mapping[str, Any]:
        """Return one validated official element-summary response."""
        self.mapped_player(player_id)
        payload = self._get_json(
            f"element-summary/{player_id}/", ttl=self.history_cache_ttl
        )
        required = {"fixtures", "history", "history_past"}
        if not isinstance(payload, Mapping) or not required.issubset(payload):
            raise OfficialFPLAPIError(
                f"Official summary for player {player_id} has an invalid schema"
            )
        return payload

    def mapped_gameweeks(self) -> List[Dict[str, Any]]:
        """Map official event state to a stable public API schema."""
        return [
            {
                "id": int(event["id"]),
                "name": event.get("name"),
                "deadline_time": event.get("deadline_time"),
                "release_time": event.get("release_time"),
                "finished": bool(event.get("finished")),
                "data_checked": bool(event.get("data_checked")),
                "is_previous": bool(event.get("is_previous")),
                "is_current": bool(event.get("is_current")),
                "is_next": bool(event.get("is_next")),
                "average_entry_score": event.get("average_entry_score"),
                "highest_score": event.get("highest_score"),
                "chip_plays": event.get("chip_plays", []),
            }
            for event in self.bootstrap()["events"]
        ]

    def mapped_teams(self) -> List[Dict[str, Any]]:
        """Map official teams and strength ratings."""
        fields = (
            "strength",
            "strength_overall_home",
            "strength_overall_away",
            "strength_attack_home",
            "strength_attack_away",
            "strength_defence_home",
            "strength_defence_away",
        )
        return [
            {
                "id": int(team["id"]),
                "code": team.get("code"),
                "name": _team_name(team.get("name")),
                "official_name": team.get("name"),
                "short_name": team.get("short_name"),
                "pulse_id": team.get("pulse_id"),
                **{field: team.get(field) for field in fields},
            }
            for team in self.bootstrap()["teams"]
        ]

    def mapped_players(
        self,
        team_id: Optional[int] = None,
        element_type: Optional[int] = None,
        selectable_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Map official player identities, availability, prices, and totals."""
        bootstrap = self.bootstrap()
        teams = {
            int(team["id"]): _team_name(team["name"]) for team in bootstrap["teams"]
        }
        positions = {
            int(position["id"]): position.get("singular_name")
            for position in bootstrap["element_types"]
        }
        players = []
        for player in bootstrap["elements"]:
            selectable = bool(
                player.get("can_select", not player.get("removed", False))
            )
            if team_id is not None and int(player["team"]) != team_id:
                continue
            if element_type is not None and int(player["element_type"]) != element_type:
                continue
            if selectable_only and not selectable:
                continue
            players.append(self._mapped_player(player, teams, positions, selectable))
        return players

    def mapped_player(self, player_id: int) -> Dict[str, Any]:
        """Map one official player or raise a typed not-found error."""
        players = self.mapped_players()
        player = next((item for item in players if item["id"] == player_id), None)
        if player is None:
            raise OfficialFPLNotFoundError(
                f"Official FPL player {player_id} was not found"
            )
        return player

    def mapped_fixtures(
        self, gameweek: Optional[int] = None, team_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Map official fixtures with named home and away teams."""
        teams = {team["id"]: team for team in self.mapped_teams()}
        results = []
        for fixture in self.fixtures():
            home_id = int(fixture["team_h"])
            away_id = int(fixture["team_a"])
            if gameweek is not None and fixture.get("event") != gameweek:
                continue
            if team_id is not None and team_id not in {home_id, away_id}:
                continue
            results.append(
                {
                    "id": int(fixture["id"]),
                    "gameweek": fixture.get("event"),
                    "kickoff_time": fixture.get("kickoff_time"),
                    "started": bool(fixture.get("started")),
                    "finished": bool(fixture.get("finished")),
                    "finished_provisional": bool(fixture.get("finished_provisional")),
                    "minutes": fixture.get("minutes"),
                    "provisional_start_time": bool(
                        fixture.get("provisional_start_time")
                    ),
                    "home_team": self._fixture_team(
                        teams.get(home_id, {}),
                        fixture.get("team_h_score"),
                        fixture.get("team_h_difficulty"),
                    ),
                    "away_team": self._fixture_team(
                        teams.get(away_id, {}),
                        fixture.get("team_a_score"),
                        fixture.get("team_a_difficulty"),
                    ),
                }
            )
        return results

    def mapped_player_summary(self, player_id: int) -> Dict[str, Any]:
        """Map official current-season history and upcoming player fixtures."""
        bootstrap = self.bootstrap()
        teams = {
            int(team["id"]): _team_name(team["name"]) for team in bootstrap["teams"]
        }
        player = self.mapped_player(player_id)
        payload = self.player_summary(player_id)
        history = [
            {
                "gameweek": item.get("round"),
                "fixture_id": item.get("fixture"),
                "kickoff_time": item.get("kickoff_time"),
                "opponent_team_id": item.get("opponent_team"),
                "opponent_team_name": teams.get(item.get("opponent_team")),
                "was_home": item.get("was_home"),
                "price": self._api_cost(item.get("value")),
                "selected": item.get("selected"),
                "transfers_in": item.get("transfers_in"),
                "transfers_out": item.get("transfers_out"),
                "total_points": item.get("total_points"),
                **self._api_stats(item),
            }
            for item in payload["history"]
        ]
        upcoming = [
            {
                "fixture_id": item.get("id"),
                "gameweek": item.get("event"),
                "kickoff_time": item.get("kickoff_time"),
                "opponent_team_id": item.get("team_a")
                if item.get("is_home")
                else item.get("team_h"),
                "opponent_team_name": teams.get(
                    item.get("team_a") if item.get("is_home") else item.get("team_h")
                ),
                "was_home": item.get("is_home"),
                "difficulty": item.get("difficulty"),
            }
            for item in payload["fixtures"]
        ]
        past_seasons = [
            {
                "season_name": item.get("season_name"),
                "start_price": self._api_cost(item.get("start_cost")),
                "end_price": self._api_cost(item.get("end_cost")),
                "total_points": item.get("total_points"),
                "minutes": item.get("minutes"),
                "goals": item.get("goals_scored"),
                "assists": item.get("assists"),
                "clean_sheets": item.get("clean_sheets"),
            }
            for item in payload["history_past"]
        ]
        return {
            "player": player,
            "history": history,
            "upcoming_fixtures": upcoming,
            "past_seasons": past_seasons,
        }

    @staticmethod
    def _mapped_player(
        player: Mapping[str, Any],
        teams: Mapping[int, str],
        positions: Mapping[int, Any],
        selectable: bool,
    ) -> Dict[str, Any]:
        return {
            "id": int(player["id"]),
            "code": player.get("code"),
            "web_name": player.get("web_name"),
            "first_name": player.get("first_name"),
            "second_name": player.get("second_name"),
            "team_id": int(player["team"]),
            "team_name": teams.get(int(player["team"])),
            "element_type": int(player["element_type"]),
            "position": positions.get(int(player["element_type"])),
            "status": player.get("status"),
            "can_select": selectable,
            "removed": bool(player.get("removed")),
            "news": player.get("news"),
            "chance_of_playing_next_round": player.get("chance_of_playing_next_round"),
            "price": OfficialFPLClient._api_cost(player.get("now_cost")),
            "selected_by_percent": OfficialFPLClient._api_number(
                player.get("selected_by_percent")
            ),
            "total_points": player.get("total_points"),
            **OfficialFPLClient._api_stats(player),
        }

    @staticmethod
    def _fixture_team(
        team: Mapping[str, Any], score: Any, difficulty: Any
    ) -> Dict[str, Any]:
        return {
            "id": team.get("id"),
            "name": team.get("name"),
            "short_name": team.get("short_name"),
            "score": score,
            "difficulty": difficulty,
        }

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
            int(team["id"]): _team_name(team["name"]) for team in bootstrap["teams"]
        }
        matches = sorted(
            (
                fixture
                for fixture in self.fixtures()
                if fixture.get("event") == gameweek
            ),
            key=lambda fixture: (
                fixture.get("kickoff_time") or "",
                fixture.get("id", 0),
            ),
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
            int(team["id"]): _team_name(team["name"]) for team in bootstrap["teams"]
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
                    logger.warning(
                        "Skipping official history for player %d: %s",
                        player_id,
                        error,
                    )

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
        return OfficialFPLClient._common_player_values(player, teams) | {
            "gameweek": int(history["round"]),
            "was_home": bool(history.get("was_home")),
            "opponent_team_name": teams.get(int(history["opponent_team"])),
            "now_cost": OfficialFPLClient._cost(history.get("value")),
            "selected_by_percent": OfficialFPLClient._number(
                player.get("selected_by_percent")
            ),
            "total_points": OfficialFPLClient._number(history.get("total_points")),
            **OfficialFPLClient._official_stats(history),
        }

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
            "goals_conceded": OfficialFPLClient._number(values.get("goals_conceded")),
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
    def _api_stats(values: Mapping[str, Any]) -> Dict[str, Any]:
        """Return JSON-safe versions of the model's mapped official stats."""
        return {
            key: None if not np.isfinite(value) else value
            for key, value in OfficialFPLClient._official_stats(values).items()
        }

    @staticmethod
    def _api_number(value: Any) -> Optional[float]:
        numeric = OfficialFPLClient._number(value)
        return numeric if np.isfinite(numeric) else None

    @staticmethod
    def _api_cost(value: Any) -> Optional[float]:
        numeric = OfficialFPLClient._cost(value)
        return numeric if np.isfinite(numeric) else None

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

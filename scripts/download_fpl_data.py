"""Download one guarded FPL Data CSV while reuse permission is pending.

The shared client is also used by optional post-GW1 inference enrichment.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import requests

from src.features import COLUMN_ALIASES


BASE_URL = "https://www.fpl-data.co.uk"
SOURCE_PAGE = f"{BASE_URL}/statistics"
DASH_UPDATE_URL = f"{BASE_URL}/_dash-update-component"
USER_AGENT = (
    "OpenFPL-Scout-AI/5.3 "
    "(+https://github.com/elcaiseri/OpenFPL-Scout-AI; permission pending)"
)
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MIN_ROWS = 100

# The non-official inputs called out in the model audit. Their presence is
# reported, not required: FPL Data's schema varies by season and currently does
# not supply every one of them.
TARGET_FEATURES = (
    "total_shots",
    "shots_on_target",
    "shots_in_box",
    "non_penalty_expected_goals",
    "non_penalty_goals",
    "chances_created",
    "expected_clean_sheet",
    "clearances",
    "shot_blocks",
    "interceptions",
    "non_penalty_expected_goal_involvements",
    "expected_points",
    "PvsxP",
    "touches",
    "touches_opp_box",
    "carries_final_third",
    "carries_penalty_area",
)

REQUIRED_COLUMNS = {
    "id",
    "element_type",
    "web_name",
    "team_name",
    "opponent_team_name",
    "was_home",
    "gameweek",
    "minutes",
    "total_points",
}


@dataclass(frozen=True)
class Season:
    label: str
    value: str
    start_year: int
    end_year: int


@dataclass(frozen=True)
class DatasetSummary:
    bytes: int
    sha256: str
    rows: int
    players: int
    min_gameweek: int
    max_gameweek: int
    latest_observed_gameweek: int
    columns: tuple[str, ...]
    canonical_columns: tuple[str, ...]
    supplied_target_features: tuple[str, ...]
    missing_target_features: tuple[str, ...]


def _callback_payload(
    output: str,
    component_id: str,
    property_name: str,
    inputs: Sequence[Mapping[str, Any]],
    state: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "output": output,
        "outputs": {"id": component_id, "property": property_name},
        "changedPropIds": [f"{inputs[0]['id']}.{inputs[0]['property']}"],
        "inputs": list(inputs),
        "state": list(state),
    }


def _find_component(value: Any, component_id: str) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        props = value.get("props")
        if isinstance(props, dict) and props.get("id") == component_id:
            return value
        for child in value.values():
            found = _find_component(child, component_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_component(child, component_id)
            if found is not None:
                return found
    return None


def _parse_season(label: str, value: str) -> Season:
    match = re.fullmatch(r"(20\d{2})_(\d{2}|20\d{2})", value)
    if not match:
        raise ValueError(f"FPL Data returned an unexpected season value: {value!r}")
    start_year = int(match.group(1))
    raw_end = match.group(2)
    end_year = int(raw_end)
    if len(raw_end) == 2:
        end_year += (start_year // 100) * 100
        if end_year <= start_year:
            end_year += 100
    if end_year != start_year + 1:
        raise ValueError(f"FPL Data returned an invalid season range: {value!r}")
    return Season(
        label=str(label), value=value, start_year=start_year, end_year=end_year
    )


class FPLDataClient:
    """Small client for the public statistics page's Dash callbacks."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = (10.0, timeout_seconds)
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Referer": SOURCE_PAGE,
            }
        )

    def _post_callback(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            DASH_UPDATE_URL, json=dict(payload), timeout=self.timeout
        )
        response.raise_for_status()
        try:
            body = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise ValueError("FPL Data returned a non-JSON callback response") from exc
        if not isinstance(body, dict) or not isinstance(body.get("response"), dict):
            raise ValueError("FPL Data returned an unexpected callback response")
        return body

    def available_seasons(self) -> list[Season]:
        payload = _callback_payload(
            output="page-content.children",
            component_id="page-content",
            property_name="children",
            inputs=({"id": "url", "property": "pathname", "value": "/statistics"},),
        )
        body = self._post_callback(payload)
        component = _find_component(body["response"], "input-year")
        if component is None:
            raise ValueError("FPL Data's statistics page has no season selector")
        options = component.get("props", {}).get("options")
        if not isinstance(options, list) or not options:
            raise ValueError("FPL Data returned no downloadable seasons")

        seasons = []
        for option in options:
            if not isinstance(option, dict):
                raise ValueError("FPL Data returned an invalid season option")
            seasons.append(
                _parse_season(option.get("label", ""), option.get("value", ""))
            )
        return sorted(seasons, key=lambda season: season.end_year, reverse=True)

    def download_csv(self, season: Season) -> tuple[bytes, str]:
        payload = _callback_payload(
            output="download-dataframe-csv.data",
            component_id="download-dataframe-csv",
            property_name="data",
            inputs=({"id": "btn_csv", "property": "n_clicks", "value": 1},),
            state=({"id": "input-year", "property": "value", "value": season.value},),
        )
        body = self._post_callback(payload)
        download = (
            body.get("response", {}).get("download-dataframe-csv", {}).get("data")
        )
        if not isinstance(download, dict):
            raise ValueError("FPL Data returned no CSV download")

        filename = download.get("filename")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or Path(filename).suffix.lower() != ".csv"
        ):
            raise ValueError("FPL Data returned an unsafe or non-CSV filename")
        content = download.get("content")
        if not isinstance(content, str):
            raise ValueError("FPL Data returned an invalid CSV payload")
        if download.get("base64"):
            try:
                raw = base64.b64decode(content, validate=True)
            except ValueError as exc:
                raise ValueError(
                    "FPL Data returned invalid base64 CSV content"
                ) from exc
        else:
            raw = content.encode("utf-8")
        if len(raw) > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"FPL Data CSV exceeds the {MAX_DOWNLOAD_BYTES}-byte safety limit"
            )
        return raw, filename


def _canonical_columns(columns: Iterable[str]) -> set[str]:
    return {COLUMN_ALIASES.get(column, column) for column in columns}


def validate_csv(raw: bytes, *, minimum_rows: int = MIN_ROWS) -> DatasetSummary:
    """Validate source identity, shape, types, and feature coverage."""
    if not raw:
        raise ValueError("Downloaded CSV is empty")
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"CSV exceeds the {MAX_DOWNLOAD_BYTES}-byte safety limit")
    if b"\x00" in raw:
        raise ValueError("Downloaded CSV contains NUL bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Downloaded CSV is not UTF-8 text") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = tuple(reader.fieldnames or ())
    if not columns or any(not column for column in columns):
        raise ValueError("Downloaded CSV has an invalid header")
    if len(set(columns)) != len(columns):
        raise ValueError("Downloaded CSV has duplicate columns")
    canonical = _canonical_columns(columns)
    missing_required = sorted(REQUIRED_COLUMNS.difference(canonical))
    if missing_required:
        raise ValueError(
            f"Downloaded CSV is missing required columns: {missing_required}"
        )

    rows = 0
    player_ids: set[int] = set()
    gameweeks: list[int] = []
    observed_gameweeks: list[int] = []
    for line_number, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"CSV row {line_number} has more values than columns")
        try:
            player_id = int(row["id"])
            element_type = int(row["element_type"])
            gameweek = int(row["gameweek"])
            minutes = float(row["minutes"])
            float(row["total_points"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"CSV row {line_number} has invalid core values") from exc
        # FPL Data's 2024/25 export includes the temporary Mystery Chip
        # managers as element type 5. Preserve those source rows; downstream
        # training already filters the football-player types 1 through 4.
        if player_id < 1 or element_type not in {1, 2, 3, 4, 5}:
            raise ValueError(f"CSV row {line_number} has invalid player identity")
        if not 1 <= gameweek <= 38 or not 0 <= minutes <= 180:
            raise ValueError(f"CSV row {line_number} has invalid match values")
        if not row.get("web_name") or not row.get("team_name"):
            raise ValueError(f"CSV row {line_number} has missing player identity")
        rows += 1
        if element_type in {1, 2, 3, 4}:
            player_ids.add(player_id)
        gameweeks.append(gameweek)
        if minutes > 0:
            observed_gameweeks.append(gameweek)

    if rows < minimum_rows:
        raise ValueError(
            f"Downloaded CSV has only {rows} rows; expected at least {minimum_rows}"
        )
    if len(player_ids) < 50:
        raise ValueError("Downloaded CSV contains fewer than 50 players")

    supplied = tuple(feature for feature in TARGET_FEATURES if feature in canonical)
    missing = tuple(feature for feature in TARGET_FEATURES if feature not in canonical)
    return DatasetSummary(
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        rows=rows,
        players=len(player_ids),
        min_gameweek=min(gameweeks),
        max_gameweek=max(gameweeks),
        latest_observed_gameweek=max(observed_gameweeks, default=0),
        columns=columns,
        canonical_columns=tuple(sorted(canonical)),
        supplied_target_features=supplied,
        missing_target_features=missing,
    )


def check_for_regression(current: DatasetSummary, incoming: DatasetSummary) -> None:
    """Reject a plausible stale, truncated, or lower-coverage replacement."""
    if incoming.latest_observed_gameweek < current.latest_observed_gameweek:
        raise ValueError(
            "Incoming CSV regresses observed gameweek coverage "
            f"({incoming.latest_observed_gameweek} < "
            f"{current.latest_observed_gameweek})"
        )
    if incoming.rows < current.rows * 0.8:
        raise ValueError(
            f"Incoming CSV row count dropped too far ({incoming.rows} < "
            f"80% of {current.rows})"
        )
    if incoming.players < current.players * 0.8:
        raise ValueError(
            f"Incoming CSV player count dropped too far ({incoming.players} < "
            f"80% of {current.players})"
        )
    lost_features = sorted(
        set(current.supplied_target_features).difference(
            incoming.supplied_target_features
        )
    )
    if lost_features:
        raise ValueError(f"Incoming CSV loses model feature columns: {lost_features}")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _resolve_season(requested: str, seasons: Sequence[Season]) -> Season:
    if requested.lower() == "latest":
        return max(seasons, key=lambda season: season.end_year)
    normalized = requested.replace("/", "_").replace("-", "_")
    for season in seasons:
        if normalized in {season.value, str(season.end_year)}:
            return season
    choices = ", ".join(f"{season.label} ({season.value})" for season in seasons)
    raise ValueError(f"Season {requested!r} is unavailable; choose one of: {choices}")


def _default_output(season: Season) -> Path:
    return Path("data/external") / f"fpl-data-stats-{season.end_year}.csv"


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--season",
        default="latest",
        help="Season value, label, end year, or 'latest' (default: latest)",
    )
    parser.add_argument("--output", type=Path, help="Override the safe default path")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing file after validation and regression checks",
    )
    parser.add_argument(
        "--allow-regression",
        action="store_true",
        help="Allow an explicit replacement with lower coverage",
    )
    parser.add_argument(
        "--acknowledge-permission-pending",
        action="store_true",
        help=(
            "Confirm this is temporary permission-pending use with provenance, "
            "attribution, no CSV redistribution, and an immediate kill switch"
        ),
    )
    parser.add_argument(
        "--list-seasons", action="store_true", help="List seasons without downloading"
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=60.0, help="Read timeout (default: 60)"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    if not args.list_seasons and not args.acknowledge_permission_pending:
        raise ValueError(
            "Permission is still pending. Re-run with "
            "--acknowledge-permission-pending for temporary guarded use."
        )

    client = FPLDataClient(timeout_seconds=args.timeout_seconds)
    seasons = client.available_seasons()
    if args.list_seasons:
        print(json.dumps([asdict(season) for season in seasons], indent=2))
        return 0

    season = _resolve_season(args.season, seasons)
    output = args.output or _default_output(season)
    raw, source_filename = client.download_csv(season)
    incoming = validate_csv(raw)

    status = "created"
    if output.exists():
        current_raw = output.read_bytes()
        if hashlib.sha256(current_raw).hexdigest() == incoming.sha256:
            status = "unchanged"
        else:
            if not args.replace:
                raise FileExistsError(
                    f"Refusing to replace {output}; inspect it and pass --replace"
                )
            current = validate_csv(current_raw)
            if not args.allow_regression:
                check_for_regression(current, incoming)
            _atomic_write(output, raw)
            status = "replaced"
    else:
        _atomic_write(output, raw)

    metadata = {
        "access_method": "public Download CSV button (Dash callback)",
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "license_status": "permission-requested-pending",
        "output": str(output),
        "season": asdict(season),
        "source_filename": source_filename,
        "source_page": SOURCE_PAGE,
        "status": status,
        **asdict(incoming),
    }
    metadata_path = output.with_suffix(".metadata.json")
    _atomic_write(
        metadata_path,
        (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

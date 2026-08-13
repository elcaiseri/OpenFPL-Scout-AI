# OpenFPL Scout API

Version: 5.1.0

OpenFPL Scout generates player projections and a positional 15-player squad.
All runtime football data comes directly from the public official Fantasy
Premier League API at `https://fantasy.premierleague.com/api/`.

## Runtime data sources

| Data | Official endpoint |
|---|---|
| Players, clubs, positions, event state | `/bootstrap-static/` |
| Fixtures, kickoff times, home/away, difficulty | `/fixtures/` |
| Per-player gameweek history | `/element-summary/{element_id}/` |

No football-data.org key, RapidAPI data feed, or uploaded statistics file is
required. Official responses are cached briefly in memory to reduce traffic.

Before the first gameweek, official current-season match history is empty.
OpenFPL therefore leaves form statistics unknown and lets each trained model's
pipeline impute them. It does not substitute a third-party preseason feed.

## Authentication

The web endpoints `GET /api/scout`, `GET /api/gameweeks`, and `GET /api/health`
are public. Product/API endpoints retain bearer-token authentication:

```http
Authorization: Bearer <API_TOKEN>
```

Set comma-separated server tokens in `VALID_API_KEYS`.

## Endpoints

### `GET /api/health`

Returns service, source, and model state.

### `GET /api/gameweeks`

Returns completed events plus the official current/next gameweek.

### `GET /api/scout?gameweek=1`

Generates a fresh squad and all player projections using official FPL data.
The gameweek is optional; when omitted, the official next/current event is used.

```bash
curl "http://localhost:8000/api/scout?gameweek=1"
```

### `POST /api/scout?gameweek=1`

Authenticated equivalent of `GET /api/scout`. It no longer accepts a CSV or
multipart upload.

```bash
curl -X POST "http://localhost:8000/api/scout?gameweek=1" \
  -H "Authorization: Bearer <API_TOKEN>"
```

### `GET /api/gw/scout?gameweek=1`

Authenticated squad-only response generated from official data.

### `GET /api/gw/playerpoints?gameweek=1`

Authenticated player projections. Optional filters are `element_type`,
`web_name`, `team_name`, and `was_home`.

## Response

```json
{
  "scout_team": [],
  "player_points": [],
  "gameweek": 1,
  "version": "5.1.0",
  "source": "official-fpl",
  "credits": "OpenFPL Scout AI | Official FPL data | @elcaiseri, 2026"
}
```

## Historical model training

The official FPL API is an active-season service and does not expose a stable,
versioned archive of every prior season. New active-season observations are
fetched through the official client and can be archived for future retraining:

```bash
uv run python -m scripts.collect_official_fpl --gameweek 39
```

Official archives are written to `data/official`, the trainer's default input.
Legacy `data/external` snapshots are not read by runtime inference or by the
default retraining command.

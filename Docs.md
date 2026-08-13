# OpenFPL Scout API

Version: 5.2.0

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

## Discovery and authentication

The complete live catalog is available at `GET /api`. Interactive OpenAPI
documentation is organized by resource at `/docs`, with ReDoc at `/redoc` and
the machine-readable schema at `/openapi.json`.

Service discovery, mapped official FPL resources, and the web scout endpoint
are public. Authenticated squad and projection routes require a bearer token:

```http
Authorization: Bearer <API_TOKEN>
```

Set comma-separated server tokens in `VALID_API_KEYS`.
For local development, `.env` is loaded automatically without overriding any
variables already supplied by the process or deployment environment.

## API tags and endpoints

### Service

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api` | Public | Full API catalog grouped by tag |
| GET | `/api/health` | Public | Service, model, and source health |

### Official FPL · Gameweeks

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/gameweeks` | Public | Completed plus current/next playable events |
| GET | `/api/fpl/gameweeks` | Public | All mapped official event records |

### Official FPL · Teams

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/teams` | Public | Clubs, normalized names, IDs, and strength ratings |

### Official FPL · Players

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/players` | Public | Players, prices, availability, and official totals |
| GET | `/api/fpl/players/{player_id}` | Public | One mapped player |
| GET | `/api/fpl/players/{player_id}/history` | Public | Match history, upcoming fixtures, and past-season totals |

`/api/fpl/players` accepts `team_id`, `element_type`, and `selectable_only`
filters. Position values are GK=1, DEF=2, MID=3, and FWD=4.

### Official FPL · Fixtures

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/fixtures` | Public | Named home/away fixtures, scores, kickoff, and difficulty |

Fixtures can be filtered by `gameweek` and official `team_id`.

### Scout AI

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/scout` | Public | Live squad plus all player projections |
| POST | `/api/scout` | Bearer | Authenticated equivalent of the public scout |
| GET | `/api/gw/scout` | Bearer | Squad-only gameweek response |
| GET | `/api/gw/playerpoints` | Bearer | Filtered player projections |

```bash
curl "http://localhost:8000/api/fpl/fixtures?gameweek=1"
curl "http://localhost:8000/api/fpl/players?team_id=1&selectable_only=true"
curl "http://localhost:8000/api/scout?gameweek=1"
```

## Response

```json
{
  "scout_team": [],
  "player_points": [],
  "gameweek": 1,
  "version": "5.2.0",
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

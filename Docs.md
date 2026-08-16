# OpenFPL Scout API

Version: 6.0.0

OpenFPL Scout generates player projections and a positional 15-player squad.
The public official Fantasy Premier League API remains the authoritative
runtime source. From GW2, missing match-stat inputs can be filled by guarded
FPL Data enrichment when the exact configured season is available.

## Runtime data sources

| Data | Official endpoint |
|---|---|
| Players, clubs, positions, event state | `/bootstrap-static/` |
| Fixtures, kickoff times, home/away, difficulty | `/fixtures/` |
| Per-player gameweek history | `/element-summary/{element_id}/` |
| Live scores, bonus processing, dream teams | `/event-status/`, `/event/{event}/live/`, `/dream-team/` |
| Public managers, transfers, and picks | `/entry/{entry_id}/...` |
| Public league standings and cups | `/leagues-classic/...`, `/leagues-h2h/...`, `/league/...` |
| Regions, set pieces, rankings, and winners | `/regions/`, `/team/set-piece-notes/`, `/stats/...`, `/winners/...` |

FPL Data's public CSV download is an optional inference-enrichment source for
the 17 audited inputs absent from official history. It never supplies player
identity or upcoming fixture context.

No football-data.org key, RapidAPI data feed, or user-uploaded statistics file
is required. Official responses are cached briefly in memory. FPL Data is
cached for six hours and failures fall back to official-only inference.

Before the first gameweek, official current-season match history is empty.
OpenFPL therefore leaves form statistics unknown and lets each trained model's
pipeline impute them. It does not substitute a third-party preseason feed.

## Discovery and authentication

The complete live catalog is available at `GET /api`. In production,
interactive OpenAPI documentation is organized by resource through ReDoc at
`/redoc`. Local development also exposes Swagger at `/docs`, with the
machine-readable schema at `/openapi.json`.

See [Official-FPL-API-Kit.md](Official-FPL-API-Kit.md) for the complete audited
upstream-to-OpenFPL route map, every query option, publication behavior, and the
official account or mutation resources that are intentionally not proxied.

Only the API catalog and routes used directly by the web UI are public. All
other API routes require a bearer token:

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
| GET | `/api/health` | Bearer | Service, model, and source health |

### Official FPL · Gameweeks

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/gameweeks` | Bearer | Completed plus current/next playable events |
| GET | `/api/fpl/gameweeks` | Public | All mapped official event records |
| GET | `/api/fpl/gameweeks/status` | Public | Bonus processing and league update state |
| GET | `/api/fpl/gameweeks/{gameweek}/live` | Bearer | Live points enriched with player identity |
| GET | `/api/fpl/dream-team` | Bearer | Official season dream team when published |
| GET | `/api/fpl/gameweeks/{gameweek}/dream-team` | Bearer | Official event dream team when published |

### Official FPL · Teams

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/teams` | Bearer | Clubs, normalized names, IDs, and strength ratings |

### Official FPL · Players

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/players` | Public | Players, prices, availability, and official totals |
| GET | `/api/fpl/players/{player_id}` | Bearer | One mapped player |
| GET | `/api/fpl/players/{player_id}/history` | Bearer | Match history, upcoming fixtures, and past-season totals |

`/api/fpl/players` accepts `team_id`, `element_type`, `selectable_only`,
`status`, `search`, `min_price`, `max_price`, `order_by`, `descending`,
`offset`, and `limit`. Position values are GK=1, DEF=2, MID=3, and FWD=4;
prices are expressed in millions.

### Official FPL · Fixtures

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/fixtures` | Public | Named home/away fixtures, scores, kickoff, and difficulty |
| GET | `/api/fpl/fixtures/{fixture_id}/stats` | Bearer | Official per-player fixture statistics |

Fixtures accept `gameweek`, official `team_id`, `future_only`, and `finished`.

### Official FPL · Managers

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/managers/{entry_id}` | Bearer | Public manager profile and favorite club |
| GET | `/api/fpl/managers/{entry_id}/history` | Bearer | Current, chip, and past-season history |
| GET | `/api/fpl/managers/{entry_id}/transfers` | Bearer | Transfers with mapped incoming/outgoing players |
| GET | `/api/fpl/managers/{entry_id}/gameweeks/{gameweek}/picks` | Bearer | Event picks after official publication |

Transfers accept `gameweek`, `offset`, and `limit`.

### Official FPL · Leagues & Cups

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/leagues/classic/{league_id}/standings` | Bearer | Classic standings with official pages and phase |
| GET | `/api/fpl/leagues/h2h/{league_id}/standings` | Bearer | Head-to-head standings |
| GET | `/api/fpl/leagues/h2h/{league_id}/matches` | Bearer | Head-to-head matches by entry or event |
| GET | `/api/fpl/leagues/{league_id}/cup-status` | Bearer | League cup qualification state |

### Official FPL · Reference & Rankings

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/fpl/regions` | Bearer | Manager country and region reference data |
| GET | `/api/fpl/set-piece-notes` | Bearer | Club set-piece notes enriched with team data |
| GET | `/api/fpl/rankings/best-private-leagues` | Bearer | Best classic private leagues |
| GET | `/api/fpl/rankings/most-valuable-teams` | Bearer | Most valuable teams |
| GET | `/api/fpl/gameweeks/{gameweek}/winners` | Bearer | Event winners when published |
| GET | `/api/fpl/phases/{phase_id}/winners` | Bearer | Phase winners when published |

### Scout AI

| Method | Endpoint | Authentication | Purpose |
|---|---|---|---|
| GET | `/api/scout` | Public | Live optimized squad |
| GET | `/api/scout/team-rating` | Public | Score a manager's published squad for a Gameweek |
| POST | `/api/scout` | Bearer | Authenticated equivalent of the public scout |
| GET | `/api/gw/scout` | Bearer | Squad-only gameweek response |
| GET | `/api/gw/playerpoints` | Bearer | Filtered player projections |

```bash
curl "http://localhost:8000/api/fpl/fixtures?gameweek=1"
curl "http://localhost:8000/api/fpl/players?team_id=1&selectable_only=true"
curl "http://localhost:8000/api/scout?gameweek=1"
curl "http://localhost:8000/api/scout/team-rating?entry_id=1234567&gameweek=1"
```

Team ratings use the latest picks made public by official FPL. When planning a
future Gameweek, OpenFPL scores that published lineup with the selected
Gameweek's projections. The 100-point rating allocates 80 points to starting-XI
quality against the budget-free AI benchmark, 10 to captaincy, and 10 to
availability.

## Response

```json
{
  "scout_team": [],
  "player_points": [],
  "gameweek": 1,
  "version": "6.0.0",
  "source": "official-fpl+fpl-data",
  "credits": "OpenFPL Scout AI | Official FPL + FPL Data when available | @elcaiseri, 2026"
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
The exact current-season file under `data/external` can be a validated local
fallback for inference enrichment but is not the default retraining input.

For a temporary permission-pending current or historical import, use the public
FPL Data download control through the guarded importer:

```bash
uv run python -m scripts.download_fpl_data \
  --season latest \
  --acknowledge-permission-pending
```

The command validates content before an atomic write, records provenance and
missing-feature coverage in a sidecar metadata file, and refuses silent
replacement or material coverage regression. Runtime accepts only the exact
configured season, begins at GW2, preserves official values, requires at least
an 80% match rate, and falls back safely when enrichment cannot be applied.
Permission remains pending; keep attribution and provenance, do not
redistribute the CSV, and disable the integration if the owner declines. Set
`FPL_DATA_INFERENCE_ENABLED=false` for an immediate production kill switch.

# Official FPL API Kit

Audited against the official Fantasy Premier League web client on 2026-08-14.
The upstream API is used by the official site but is not a separately versioned
public developer product, so OpenFPL validates and caches its responses behind a
stable, read-only mapping layer.

Base URLs:

- OpenFPL: `http://localhost:8000/api/fpl`
- Official source: `https://fantasy.premierleague.com/api`
- Complete generated OpenFPL catalog: `GET /api`
- Production API reference: `GET /redoc` (Swagger at `GET /docs` is available
  in local development only)

Every mapped response identifies its `source` and `official_endpoint`.
Collections use `count`, `total`, and `results`; object resources use `data`.

## Gameweeks and live scoring

| OpenFPL route | Official source | Notes |
|---|---|---|
| `GET /api/gameweeks` | `/bootstrap-static/` | Finished plus current/next playable events |
| `GET /api/fpl/gameweeks` | `/bootstrap-static/` | All 38 mapped events |
| `GET /api/fpl/gameweeks/status` | `/event-status/` | Bonus processing and league update state |
| `GET /api/fpl/gameweeks/{gameweek}/live` | `/event/{gameweek}/live/` | Live points and explanations enriched with player identity |
| `GET /api/fpl/dream-team` | `/dream-team/` | Season dream team when officially published |
| `GET /api/fpl/gameweeks/{gameweek}/dream-team` | `/dream-team/{gameweek}/` | Gameweek dream team when published |

`gameweek` accepts `1..38`. Dream-team endpoints return `404` until the official
service publishes the resource.

## Teams and players

| OpenFPL route | Official source |
|---|---|
| `GET /api/fpl/teams` | `/bootstrap-static/` |
| `GET /api/fpl/players` | `/bootstrap-static/` |
| `GET /api/fpl/players/{player_id}` | `/bootstrap-static/` |
| `GET /api/fpl/players/{player_id}/history` | `/element-summary/{player_id}/` |

Player collection options:

| Option | Values/default | Purpose |
|---|---|---|
| `team_id` | Official positive integer | Filter by club |
| `element_type` | `1..4` | GK=1, DEF=2, MID=3, FWD=4 |
| `selectable_only` | `false` | Exclude unavailable-to-select records |
| `status` | One official status character | Filter official availability state |
| `search` | Text | Search web, first, and second names |
| `min_price`, `max_price` | Decimal millions | Inclusive price range |
| `order_by` | `id`, `web_name`, `price`, `total_points`, `selected_by_percent` | Sort field |
| `descending` | `false` | Reverse populated sort values |
| `offset` | `0` | Pagination offset |
| `limit` | `100`, maximum `1000` | Page size |

## Fixtures

| OpenFPL route | Official source |
|---|---|
| `GET /api/fpl/fixtures` | `/fixtures/`, including official `event` and `future` capabilities |
| `GET /api/fpl/fixtures/{fixture_id}/stats` | `/fixture/{fixture_id}/stats/` |

Fixture collection options are `gameweek=1..38`, `team_id`, `future_only`, and
`finished`. Home and away records include official IDs, normalized names,
scores, and difficulty.

## Public managers

| OpenFPL route | Official source |
|---|---|
| `GET /api/fpl/managers/{entry_id}` | `/entry/{entry_id}/` |
| `GET /api/fpl/managers/{entry_id}/history` | `/entry/{entry_id}/history/` |
| `GET /api/fpl/managers/{entry_id}/transfers` | `/entry/{entry_id}/transfers/` |
| `GET /api/fpl/managers/{entry_id}/gameweeks/{gameweek}/picks` | `/entry/{entry_id}/event/{gameweek}/picks/` |

Transfer options are `gameweek`, `offset`, and `limit` (maximum `1000`). The
official service controls deadline privacy: current picks and transfers may
return `404` or remain hidden until their deadline passes.

## Leagues and cups

| OpenFPL route | Official source |
|---|---|
| `GET /api/fpl/leagues/classic/{league_id}/standings` | `/leagues-classic/{league_id}/standings/` |
| `GET /api/fpl/leagues/h2h/{league_id}/standings` | `/leagues-h2h/{league_id}/standings/` |
| `GET /api/fpl/leagues/h2h/{league_id}/matches` | `/leagues-h2h-matches/league/{league_id}/` |
| `GET /api/fpl/leagues/{league_id}/cup-status` | `/league/{league_id}/cup-status/` |

Classic standings accept `page_standings`, `page_new_entries`, and `phase`.
Head-to-head standings accept both page options. Head-to-head matches accept
`page`, optional `entry_id`, and optional `gameweek`.

## Reference, winners, and rankings

| OpenFPL route | Official source |
|---|---|
| `GET /api/fpl/regions` | `/regions/` |
| `GET /api/fpl/set-piece-notes` | `/team/set-piece-notes/` |
| `GET /api/fpl/rankings/best-private-leagues` | `/stats/best-classic-private-leagues/` |
| `GET /api/fpl/rankings/most-valuable-teams` | `/stats/most-valuable-teams/` |
| `GET /api/fpl/gameweeks/{gameweek}/winners` | `/winners/event/{gameweek}/` |
| `GET /api/fpl/phases/{phase_id}/winners` | `/winners/phase/{phase_id}/` |

Winner and ranking tables are empty or `404` until the official service
publishes them for the active season.

## Scout AI

| Method and route | Authentication | Options |
|---|---|---|
| `GET /api/scout` | Public | Optional `gameweek` |
| `POST /api/scout` | OpenFPL bearer token | Optional `gameweek` |
| `GET /api/gw/scout` | OpenFPL bearer token | Required `gameweek` |
| `GET /api/gw/playerpoints` | OpenFPL bearer token | `gameweek`, `element_type`, `web_name`, `team_name`, `was_home` |

## Intentionally not proxied

The current official web client also calls account-bound or state-changing
resources. OpenFPL does not proxy them because doing so would require official
FPL cookies/CSRF credentials or would mutate a user's team:

- Account reads: `/me/`, `/my-team/{entry_id}/`,
  `/entry/{entry_id}/transfers-latest/`, `/leagues-renewable/`
- Team mutations: `/transfers/`, `/my-team/{entry_id}/`, `/entry-create/`,
  `/entry-update/`, `/entry-autopick/`
- League mutations and private administration: create, join, leave, renew,
  delete, invite-code, ban, and unban endpoints
- User watchlist and entry-image upload endpoints

Non-football content services used by the website, such as community-wall and
Adobe access tokens, are also outside the OpenFPL data API.

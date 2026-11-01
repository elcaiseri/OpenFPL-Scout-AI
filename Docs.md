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
refreshed every six hours and failures fall back to the last good dataset or
to official-only inference.

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

## MCP server

The same service is a remote [Model Context Protocol](https://modelcontextprotocol.io)
server at `/mcp` (Streamable HTTP, stateless, JSON responses), so Claude and
other MCP clients can call OpenFPL as tools. Each tool runs the handler of the
REST route listed beside it, with the same validation, data, and errors.

| Tool | Access | Same as | Arguments |
| --- | --- | --- | --- |
| `get_scout_squad` | Free | `GET /api/scout` | `gameweek` (optional) |
| `rate_team` | Free | `GET /api/scout/team-rating` | `entry_id`, `gameweek` |
| `get_gameweeks` | Free | `GET /api/fpl/gameweeks` | none |
| `get_fixtures` | Free | `GET /api/fpl/fixtures` | `gameweek` (optional) |
| `get_player_predictions` | API key | `GET /api/gw/playerpoints` | `gameweek`, optional `element_type`, `web_name`, `team_name`, `was_home`, `limit` |
| `get_live_points` | API key | `GET /api/fpl/gameweeks/{gameweek}/live` | `gameweek`, `limit` |
| `get_set_piece_notes` | API key | `GET /api/fpl/set-piece-notes` | none |
| `get_manager_picks` | API key | `GET /api/fpl/managers/{entry_id}/gameweeks/{gameweek}/picks` | `entry_id`, `gameweek` |

The two tools that cover every player keep a model's context small:
`get_player_predictions` sorts by `expected_points` and `get_live_points` by
`total_points`, each returning the top `limit` (default 50, maximum 1000) with
`total` reporting how many matched. Every tool is read-only.

### Sign-in

Every request to `/mcp` needs a token; without one it answers `401` with OAuth
discovery headers, which is what makes Claude open the sign-in. OpenFPL is its
own OAuth 2.1 server (dynamic client registration, PKCE), so there is no
third-party identity provider. Signing in opens `/oauth/connect` on this site,
where the person either:

- pastes an OpenFPL API key (one of `VALID_API_KEYS`) to unlock all 8 tools, or
- chooses **Continue with free tools** for the 4 free ones.

A free connection that calls an API-key tool gets a tool error telling the
person to reconnect with a key. A raw API key also works as the bearer token,
for clients that send a fixed header. Removing a key from `VALID_API_KEYS`
immediately stops every sign-in made with it.

Nothing is stored server-side: client registrations, codes, and tokens are
signed with `OPENFPL_OAUTH_SECRET`. Set it to a long random value (for
example `openssl rand -base64 48`) and keep it the same on every instance.
Without it each process picks a random secret, and every sign-in breaks on a
restart or on another instance. The issuer is `OPENFPL_SITE_URL`
(default `https://openfpl.kassem.dev`). Access tokens last an hour and
refresh for 30 days.

`GET /mcp` returns `405`: a stateless server has no stream to offer.

### Connect from Claude

**Claude (claude.ai, desktop, and mobile).** In Settings → Connectors, add a
custom connector with the URL `https://openfpl.kassem.dev/mcp` and leave the
OAuth fields empty. Claude opens the OpenFPL sign-in page; enter an API key
or continue with the free tools. To switch tiers later, disconnect and
connect again.

**Claude Code.** Add the server and sign in from `/mcp` in a session, which
opens the same page:

```bash
claude mcp add --transport http openfpl https://openfpl.kassem.dev/mcp
```

Or skip the browser and send a key directly with
`--header "Authorization: Bearer <API_TOKEN>"`.

**Claude API.** The Messages API's MCP connector sends a key as
`authorization_token`, with the `mcp-client-2025-11-20` beta:

```python
response = client.beta.messages.create(
    model="claude-opus-5-5",
    max_tokens=16000,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[{
        "type": "url",
        "url": "https://openfpl.kassem.dev/mcp",
        "name": "openfpl",
        "authorization_token": API_TOKEN,
    }],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "openfpl"}],
    messages=[{"role": "user", "content": "Who should I captain in GW10?"}],
)
```

Locally, run with `OPENFPL_SITE_URL=http://localhost:8000` so the OAuth
issuer matches; the endpoint is `http://localhost:8000/mcp`, and the MCP
Inspector (`npx @modelcontextprotocol/inspector`) can sign in and call the tools.

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
availability. A picked player FPL no longer lets managers select (for example,
after leaving the league) is scored with zero points and zero availability and
is marked `projection_missing`.

Projections are made per fixture: double-gameweek players receive the sum of
both matches, and players whose club has no fixture receive zero. Model points
are multiplied by `availability_factor`, the player's current official chance
of playing, and players with zero availability are never selected.

A double-gameweek player's `opponent_team_name` joins both opponents
(`"Chelsea / Tottenham"`). `was_home` is set only when every fixture shares the
venue; with one home and one away match it is `null` and each opponent carries
its venue (`"Chelsea (H) / Tottenham (A)"`).

## Response

```json
{
  "scout_team": [],
  "player_points": [],
  "gameweek": 1,
  "version": "6.0.0",
  "source": "official-fpl+fpl-data",
  "frozen": true,
  "forecast_captured_at": "2026-08-29T09:58:12+00:00",
  "freeze_method": "deadline",
  "credits": "OpenFPL Scout AI | Official FPL + FPL Data when available | @elcaiseri, 2026"
}
```

`frozen` is true once the Gameweek's deadline has passed: the response is the
frozen forecast, made at `forecast_captured_at`, and it does not change on
later requests. `freeze_method` says how it was frozen: `deadline` for the
forecast captured before the deadline, or `after-deadline` for a closed
Gameweek that had no such forecast (for example, one that closed before frozen
forecasts shipped) and was frozen on its first recall. That freeze keeps an
older version's forecast when its archive proves it predates the deadline, and
otherwise the prediction made on that recall. Open Gameweeks are predicted live
and report `frozen: false`, as do closed ones when the archive is disabled.
Player status shown alongside a frozen squad (for example on the dashboard) is
current, but the picks and expected points are the ones made before the
deadline.

## Historical model training

The official FPL API is an active-season service and does not expose a stable,
versioned archive of every prior season. New active-season observations are
fetched through the official client and can be archived for future retraining:

```bash
uv run python -m scripts.collect_official_fpl --gameweek 39
```

Official archives are written to `data/official`, the trainer's default input.
Played rows leave `selected_by_percent` empty because Official FPL exposes only
current ownership; use `official_selected` with `total_players` from the
metadata for point-in-time ownership.
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

The running service downloads from FPL Data itself because
`fpl_data_inference.acknowledge_permission_pending` is true in
`config/config.yaml`, the same explicit acknowledgement the importer requires.
`permission_status: granted` also allows downloads. Set
`FPL_DATA_ACKNOWLEDGE_PERMISSION_PENDING=false` to use only imported local
files, whatever the permission status; `/api/health` reports the result as
`remote_downloads_allowed`. One request
refreshes the dataset while others keep using the previous one.

Runtime refreshes follow the importer's safety rules:

- A download with any fewer rows or players than the dataset in use, an
  earlier latest gameweek, or lost feature columns is rejected (the importer
  allows 20% churn; an unattended refresh allows none, except up to 5% for a
  download that adds a newer gameweek, such as one without a departed player).
  The current dataset and the file on disk stay in place, and `/api/health`
  reports `refresh_error`.
- A failed refresh keeps the dataset already in memory. The local copy is a
  fallback only when nothing is loaded yet, so an older file never replaces
  newer data.
- The CSV and its checksum metadata are committed together; if the metadata
  cannot be written, the previous CSV is restored. Identical downloads are not
  rewritten, although outdated provenance metadata is refreshed.
- Official history shows a gameweek as its fixtures are played, before FPL Data
  publishes it. Up to `max_gameweek_lag` (default 1) gameweeks of lag are
  tolerated: covered gameweeks are enriched, newer ones stay official-only and
  are listed as `unenriched_gameweeks`. A source further behind is `stale`.
- The match rate is measured over covered rows only. `unmatched_opponents`
  lists the opponents of unmatched rows, so a club-name mismatch is visible.
- A configured season that differs from the official season reports
  `season-mismatch` and is never downloaded or merged. Update
  `fpl_data_inference.season` when a new season starts. If the official season
  cannot be read, the check fails closed and reports `season-unverified`.

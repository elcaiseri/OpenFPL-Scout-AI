# OpenFPL Scout AI Team API

Version: 7.0.0

OpenFPL Scout AI Team generates player projections and a positional 15-player squad.
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

The public `POST /api/scout/sync-results` collects official match results into
the archive and returns a `status` (`saved`, `throttled`, `disabled`, or
`failed`). The dashboard starts it after displaying a saved squad; its
five-minute cooldown limits repeated collection. Public squad and team-rating
reads do not wait for this collection. Live predictions still archive their
inputs and forecasts before returning.

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

A rating also returns `bank` (the manager's bank at the last deadline, in £m)
and `transfers_open`, which is true while the Gameweek's official deadline is
ahead, whether or not its forecast is frozen. While it is, `transfers` lists
up to three single transfers, best first. Each swaps one player for a player
in the same position whom current official data lets a manager buy to play
(selectable, not injured, suspended or departed, chance of playing above
zero), costs at most the bank plus the outgoing player's current price, and
leaves every club at three players or fewer; FPL does not publish selling
prices, so affordability is approximate. `gain` is the change in the squad's
best XI plus captain, and `cost` the price difference. `clubs_over_limit`
names clubs with more than three of the squad's players, which a real-life
transfer can cause: FPL then requires the next transfer out to come from that
club, so only those sales are suggested.

Projections are made per fixture: double-gameweek players receive the sum of
both matches, and players whose club has no fixture receive zero. Model points
are multiplied by `availability_factor`, the player's current official chance
of playing, and players with zero availability are never selected.

A double-gameweek player's `opponent_team_name` joins both opponents
(`"Chelsea / Tottenham"`). `was_home` is set only when every fixture shares the
venue; with one home and one away match it is `null` and each opponent carries
its venue (`"Chelsea (H) / Tottenham (A)"`).

### Owner dashboard (private)

The `/admin` page and the routes below are for the service owner. Every route
needs a bearer token, sends `Cache-Control: no-store`, and is hidden from the
OpenAPI schema and `GET /api`. Set `OPENFPL_ADMIN_ENABLED=false` to answer the
page and every route with 404.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/admin/overview` | Service, process, gameweek, model, cache, archive, request and upstream summary |
| GET | `/api/admin/models` | Per-model status and feature contract, agreement, feature coverage and enrichment |
| GET | `/api/admin/predictions` | Every player's projection, each model's projection, the 5-match inputs and the selection decision |
| GET | `/api/admin/players/{player_id}/explain` | Input effects against a typical player in his position, fixture sensitivity and the selection reasoning |
| GET | `/api/admin/squad` | The AI squad with the reason for every pick, cut-offs, near misses and the best XI |
| GET | `/api/admin/accuracy` | Archived forecasts of played gameweeks against official points (`played_only`) |
| GET | `/api/admin/timeline` | Prediction runs, what changed in the squad and why, and admin actions |
| GET | `/api/admin/logs` | Recent log records (`level`, `q`, `logger`, `limit`) |
| GET | `/api/admin/metrics` | Per-route requests and latency, official FPL calls and cache, in-memory state |
| GET | `/api/admin/archive` | Files the season archive holds per gameweek |
| GET | `/api/admin/config` | Effective configuration and environment flags (never secrets) |
| POST | `/api/admin/predictions/recompute` | Predict an open gameweek again now (409 for a frozen one) |
| POST | `/api/admin/archive/collect` | Collect official results, optionally skipping the cooldown (`force`) |
| POST | `/api/admin/cache/clear` | Clear the official response, fixture and live forecast caches |
| POST | `/api/admin/models/{name}` | Set a model's runtime `weight` or `enabled` flag (409 below `minimum_successful_models`) |
| POST | `/api/admin/models/reset` | Remove every runtime model override |
| POST | `/api/admin/enrichment` | Turn FPL Data enrichment on or off |
| POST | `/api/admin/simulate` | Sandbox: re-select the squad with custom weights, locks, exclusions, club limit and availability |
| GET | `/api/admin/managers/{entry_id}/plan-context` | Estimated free transfers (with how), selling prices, bank, budget and chips for planning (409 once the gameweek's deadline has passed) |
| POST | `/api/admin/optimize` | Plan `transfers` (over `horizon` 1–5 gameweeks, default 3, one plan per transfer count up to `max_transfers`, 4-point hits beyond `free_transfers`), a `wildcard` (5 gameweeks) or a `freehit` (1 gameweek), from the published squad or a `draft`, honouring `overrides` (locks, bans, own availability and xP) |
| POST | `/api/admin/managers/{entry_id}/team` | A manager's working squad (`draft`: picks, chip, free transfers, bank override, purchase prices) and `overrides`: the working and published ratings, rule checks, transfers, bank and hits, expected points with autosubs, lineup and bench advice, captaincy options, transfer suggestions, and news and price pressure per player. A draft needs an open gameweek (409 otherwise) |
| POST | `/api/admin/managers/{entry_id}/outlook` | For the next `horizon` gameweeks (1–8, default 5): the squad's fixtures, difficulty and projected points, and what Bench Boost, Triple Captain and Free Hit would add each week, with the best available week |
| GET | `/api/admin/managers/{entry_id}/review` | A played `gameweek` (default the latest): points, captain against the best captain in hindsight, bench, the best XI in hindsight, transfers that week and since (with hits), and the archived forecast against actual points |

The optimizer maximises each gameweek's best legal XI plus captain (plus a
tenth of the bench) within the budget, the positional quotas and three per
club, using scipy's mixed-integer solver. A manager's budget is the bank
plus the squad's selling value. Free transfers and selling prices are
estimated from public history: one free transfer a gameweek, at most five
banked, hits counted from `event_transfers_cost`, Wildcard and Free Hit weeks
keeping banked transfers; selling prices follow FPL's sell-on rule from the
latest purchase price outside Free Hit weeks. Both can be overridden. After
the first planned gameweek doubtful players count as fit, while players
ruled out stay at zero. Public picks reflect the last deadline, so transfers
already made for the coming gameweek are not seen unless the manager adds
them as a working squad.

The working squad and personal overrides are kept in the owner's browser
(localStorage) and sent with each request; the server stores neither. Incoming
players are bought at today's price and outgoing ones sold at their estimated
selling price; hits are counted beyond the free transfers unless a Wildcard
or Free Hit is set. An own availability replaces FPL's chance of playing (a
player ruled out is re-projected as fit with the sandbox) and applies to every
planned gameweek; an own xP % scales the projection. Expected points with
autosubs treat each player's availability as his chance of playing (zero
without a fixture), bring bench players on in order when the formation stays
legal, and pass the armband to the vice; the result is exact over up to ten
uncertain players and sampled with a fixed seed beyond that. Price pressure is
net transfers this gameweek as a share of estimated owners (a heuristic:
FPL's price algorithm is not public).

Runtime changes last until the service restarts and are recorded in the
timeline. Explanations re-run the live ensemble on the rows the models saw:
in memory for gameweeks this instance predicted, otherwise rebuilt from the
archived pre-gameweek history (or current official history) with current
availability, and then marked `approximate`.

Each forecast's `inference` metadata now also records `model_predictions`
(every model's projection per player, on the served scale) and
`feature_coverage` (which inputs had data). Both are archived with the
forecast, so each model's accuracy can be checked once its gameweek is played.
Runtime model overrides are recorded in the archived model versions, so a
forecast made with them is never restored after a restart.

## Response

```json
{
  "scout_team": [],
  "player_points": [],
  "gameweek": 1,
  "version": "7.0.0",
  "source": "official-fpl+fpl-data",
  "frozen": true,
  "forecast_captured_at": "2026-08-29T09:58:12+00:00",
  "freeze_method": "deadline",
  "credits": "OpenFPL Scout AI Team | Official FPL + FPL Data when available | Islam Kassem (kassem.dev), 2026"
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

Each `scout_team` player also carries its current official `team_id`,
`position`, `price`, `selected_by_percent`, `status`, `can_select`, `news`,
`chance_of_playing_next_round` and `total_points`; predicted values win where
both have a field. On a frozen squad these details are current, but the picks
and expected points are the ones made before the deadline.

An open Gameweek's live prediction is reused for five minutes
(`inference.prediction_cache_ttl_seconds`), so requests do not rerun the
ensemble. Any change to official availability (status, chance of playing,
selectability) drops it at once. Every prediction runs inside a request:
the public squad may be answered with a forecast up to fifteen minutes old
(`prediction_stale_seconds`) and `refresh_due: true`, and the page then calls
`POST /api/scout/refresh?gameweek=N`, which predicts in its own request and
returns `fresh`, `refreshed` or `not-cached`. Other callers predict first, or
wait for a prediction already under way. After a cold start, the public
squad may be answered the same way from the open Gameweek's archived
forecast, if it is under an hour old (`prediction_restore_max_age_seconds`),
was made by the running models, and availability has not changed since.

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

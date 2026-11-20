# OpenFPL Scout AI Team

<img src="assets/openfpl-predictive-lion-frameless-2026-512.png" alt="OpenFPL Scout AI Team logo" width="160">

OpenFPL predicts Fantasy Premier League player points and selects a complete
15-player squad for each Gameweek. It combines live official FPL data with a
Ridge, XGBoost, CatBoost, and MLP ensemble, then presents the result through a
responsive web dashboard and FastAPI service.

[Live app](https://openfpl.kassem.dev) ·
[API reference](https://openfpl.kassem.dev/redoc) ·
[Route catalog](https://openfpl.kassem.dev/api) ·
[TikTok](https://www.tiktok.com/@openfpl.kassem.dev) ·
[X](https://x.com/OpenFPLScoutAI) ·
[YouTube](https://youtube.com/@openfplscoutai)

## Highlights

- Official FPL is the source of truth for players, clubs, availability,
  Gameweeks, fixtures, history, live scores, managers, leagues, and rankings.
- A four-model ensemble produces player projections from leakage-safe recent
  form, minutes, ownership, and fixture context. Each fixture is projected
  separately, so double-gameweek players receive both matches and
  blank-gameweek players receive zero.
- Projections are scaled by each player's current official availability.
  Injured, suspended, and departed players are never selected.
- Inference validates each model's feature contract, caches upstream data, and
  can continue when one model fails.
- The squad selector enforces the official positional quotas and a maximum of
  three players per club, then assigns captain and vice-captain.
- GW1 uses an explicit ownership and availability cold start when no genuine
  current-season match history exists.
- The dashboard includes Gameweek planning, deadline status, fixture context,
  pitch and table views, detailed player cards, and a manager team-rating mode.
- A public FPL team ID can be scored from 0–100 against the same AI benchmark,
  with separate starting-XI, captaincy, and availability signals, plus up to
  three affordable transfers that raise its projection.
- The Gameweek and a rated team ID stay in the address bar (`?gw=`,
  `?team=`), so any view can be bookmarked or shared.
- Optional FPL Data enrichment can fill missing historical statistics from GW2
  without replacing official values.

Squad selection is intentionally budget-free. Prices are returned for context
but do not affect player projections or selection.

## Run locally

Requirements: Python 3.9 or newer and
[uv](https://docs.astral.sh/uv/). Model artifacts must exist at the paths in
`config/config.yaml`; generated models are not stored in Git.

```bash
uv sync
uv run uvicorn main:app --reload
```

Run the test suite with:

```bash
uv run python -m unittest discover -s tests
node --test tests/frontend_loading.test.cjs tests/admin_dashboard.test.cjs
```

Open [localhost:8000](http://localhost:8000). Local Swagger documentation is
available at [localhost:8000/docs](http://localhost:8000/docs).

Protected routes read comma-separated bearer tokens from `.env` or the process
environment:

```dotenv
VALID_API_KEYS=local-development-token
OPENFPL_ENV=development
```

Optional FPL Data enrichment can be disabled immediately with:

```dotenv
FPL_DATA_INFERENCE_ENABLED=false
```

FPL Data enrichment is on by default: `config/config.yaml` records the
owner's acceptance of the pending reuse permission
(`acknowledge_permission_pending: true`), so the service downloads the
configured season itself. To use only files imported with the guarded CLI
below, whatever the permission status, set:

```dotenv
FPL_DATA_ACKNOWLEDGE_PERMISSION_PENDING=false
```

## Docker

The image intentionally excludes generated data and model artifacts. Mount the
data directory read-write and the model directory read-only at their configured
runtime paths:

```bash
docker build --platform linux/amd64 -t openfpl-scout-ai .
docker run --rm \
  --name openfpl-scout-ai \
  -p 8000:8000 \
  -e VALID_API_KEYS=local-development-token \
  --mount type=bind,src="${PWD}/data",dst=/app/data \
  --mount type=bind,src="${PWD}/models",dst=/app/models,readonly \
  openfpl-scout-ai
```

The container defaults to port `8000` and honors the `PORT` environment
variable supplied by Cloud Run. A Cloud Run revision must expose equivalent
volumes at `/app/data` and `/app/models`; local Docker bind mounts are not
transferred with the image. The model volume can be read-only, but the data
volume and its bucket permissions must allow the service to create and replace
objects.

For a low-traffic Cloud Run service, start with request-based billing, 1 vCPU,
512 MiB, concurrency 4, scale-to-zero, and one instance at most. The training
archive and frozen forecasts assume a single instance writes the data volume:

```bash
gcloud run services update SERVICE \
  --region REGION \
  --cpu 1 \
  --memory 512Mi \
  --concurrency 4 \
  --min 0 \
  --max 1 \
  --cpu-throttling \
  --cpu-boost
```

Increase memory only if Cloud Monitoring reports pressure or out-of-memory
restarts. Use a minimum instance only when lower cold-start latency is worth
the idle charge.

On Cloud Run (detected by `K_SERVICE`), application logs are one JSON object
per line with a `severity` field, so Cloud Logging can filter warnings and
errors and keeps each traceback in a single entry. Set
`OPENFPL_LOG_FORMAT=text` or `OPENFPL_LOG_FORMAT=json` to choose explicitly;
local runs default to text.

The page and every `/static` file are sent with `Cache-Control: no-cache`, so
browsers check for a newer copy (a quick 304 when nothing changed) instead of
reusing an old deploy's files. The page links `style.css` and `script.js` with
a content hash (`?v=…`), so a new deploy's page always loads its own stylesheet
and script. Images under `/assets` are cached for a day; give a changed image
a new file name to show it immediately.

## API

The web application and its supporting read endpoints are public. Administrative
and extended data routes require `Authorization: Bearer <token>`.

```bash
curl "https://openfpl.kassem.dev/api/scout?gameweek=1"
curl "https://openfpl.kassem.dev/api/scout/team-rating?entry_id=1234567&gameweek=1"
curl -X POST "https://openfpl.kassem.dev/api/scout/sync-results"
curl -X POST "https://openfpl.kassem.dev/api/scout/refresh?gameweek=1"
curl -H "Authorization: Bearer <token>" \
  "https://openfpl.kassem.dev/api/health"
```

| Area | Coverage |
|---|---|
| Scout | Player projections, full squad, captaincy, and published manager team ratings |
| Gameweeks | Event state, live scoring, and dream teams |
| Players and clubs | Search, availability, prices, history, and strength data |
| Fixtures | Opponents, venue, scores, kickoff, difficulty, and player stats |
| Managers | Profiles, season history, transfers, and published picks |
| Leagues | Classic and head-to-head standings, matches, and cup status |
| Reference data | Regions, set pieces, rankings, and winners |

See [Docs.md](Docs.md) for authentication and response details, or the
[Official FPL API Kit](Official-FPL-API-Kit.md) for the complete route map.

The dashboard shows saved squads before collecting archived match results.
After rendering a frozen squad it starts `POST /api/scout/sync-results`
without waiting for it; collection retains the archive's five-minute cooldown
and finishes inside that HTTP request, so request-based Cloud Run CPU remains
available. A squad answered from an older cached forecast carries
`refresh_due: true`; the page then starts `POST /api/scout/refresh` the same
way, so the new prediction runs inside a request too. The server never
predicts outside a request. Temporary read failures retry twice, and
unavailable player metadata or fixtures do not hide an otherwise valid squad.

## Owner dashboard

`/admin` is a private control room for the service owner. The page holds no
data: it asks for one of the `VALID_API_KEYS` tokens (kept for the browser
session, or on the device if you tick *Remember*) and reads everything from
bearer-protected `/api/admin/*` routes. Those routes are left out of the
public OpenAPI schema and the `/api` catalog, the page is sent with
`X-Robots-Tag: noindex`, and `OPENFPL_ADMIN_ENABLED=false` answers both
with 404.

| Tab | What it shows |
|---|---|
| Command centre | Health tiles, the public squad, the AI decision log (every prediction run and what changed in the squad, and why), forecast cache, recent problems and live controls |
| Predictions | Every player's projection with each model's view, the model spread and the 5-match inputs (minutes, xG, xA, xGI, shots, defensive contribution, FPL xP…), filters, charts and CSV export |
| AI squad | The pitch, the reason behind every pick, captaincy margins, position cut-offs, near misses and players kept out by the club limit, compared with the official dream team |
| My team | A public FPL team as it really is. **Squad:** edit a working squad on the pitch with the changes FPL can't see yet (transfers already made, substitutions, bench order, captain and vice, chip, free transfers, bank, what you paid), then see its rating beside the published one, expected points with autosubs, lineup and bench advice, captaincy options, news and price alerts since your last visit, and your own locks, bans, availability and xP. **Plan:** an optimizer (the best transfers for your free transfers over 1–5 gameweeks with hits, a Wildcard squad for the next 5 gameweeks, or a Free Hit squad for this one, each move explained), a chip planner and a fixture ticker. **Review:** a played gameweek's captain, bench, lineup and transfers against hindsight, plus season history and picks. **Leagues:** standings and cups |
| Model health | Per-model status, weights and feature contract, model agreement, feature coverage, FPL Data enrichment, and archived forecasts checked against actual points beside the offline training metrics |
| What-if | A sandbox: re-select the squad with your own model weights, locked or excluded players, club limit and availability, without touching the public site |
| FPL data | Fixture difficulty, results and fixture stats, live gameweek, players, club strength, dream teams, set pieces, winners and rankings |
| App health | Per-route request counts and latencies, the official FPL upstream and its cache, logs, the archive and the effective configuration |
| API explorer | Call any OpenFPL route with your token and inspect the response |

Clicking any player opens an explanation: each input's effect against a
typical player in his position, the projection's path from that typical
player, every model's projection, how the projection would change against
each opponent, his season so far, and why the AI picked him or not.

The optimizer finds the provably best squad (scipy's integer-programming
solver) within the budget, positions and three-per-club rule, maximising each
gameweek's best XI plus captain. Official FPL does not publish free
transfers or selling prices, so both are estimated from public history (one
free transfer a gameweek, at most five banked; FPL's sell-on rule) and can be
corrected before running. Plans never change the team on FPL.

FPL publishes a manager's picks only after each deadline, so My team's
working squad lets the manager add what they have already changed. It is
kept in that browser only (per team ID, with JSON export and import), checked
against FPL's rules, and cleared automatically once FPL publishes that
gameweek's team. Expected points with autosubs assume only flagged players
and blanks miss out; the price outlook is a heuristic from net transfers,
since FPL's price algorithm is not public.

Live controls (re-predict an open gameweek now, force results collection,
clear caches, set a model's weight or turn it off, turn FPL Data enrichment
on or off) ask for confirmation, are logged as warnings and in the decision
log, and last until the service restarts. A closed gameweek's forecast stays
frozen: it cannot be predicted again.

## Models and data

Runtime data flows from official FPL through the shared feature pipeline, model
ensemble, and squad selector. The optional enrichment layer accepts only the
configured season, fills missing values only, and falls back to official-only
inference on failure. FPL Data usually publishes a gameweek after it appears in
official history, so a one-gameweek lag is tolerated: covered gameweeks are
enriched and the newest stays official-only. Sources further behind, poorly
matched, or from another season are rejected, and a download never replaces a
more complete dataset.

Every successful scout inference also maintains a durable, season-scoped
archive under `data/archive/<season>/`. The archive is fail-open, so a temporary
storage failure is reported in `/api/health` without taking predictions down.
Set `OPENFPL_DATA_ARCHIVE_ENABLED=false` to disable it or
`OPENFPL_DATA_ROOT=/data` when the Cloud Run volume is mounted at `/data`
instead of `/app/data`. This single override relocates both the archive and the
durable enrichment source. `OPENFPL_DATA_ARCHIVE_ROOT` can override only the
archive location when needed.

```text
data/
├── external/                         # durable complete FPL Data source + metadata
└── archive/2026-2027/
    ├── official/
    │   ├── snapshots/gw_03/          # raw bootstrap and fixture payloads
    │   ├── live/gw_02.json           # complete event-live player payload
    │   ├── history/before_gw_03.csv  # cumulative official history
    │   └── player-stats/gw_02.csv    # normalized player stats per played GW
    ├── enriched/
    │   ├── history/before_gw_03.csv
    │   └── player-stats/gw_02.csv
    ├── predictions/gw_03.csv
    ├── squads/gw_03.json
    └── metadata/gw_03.json
```

Official history CSVs retain the normalized model fields and every raw
gameweek-history field with an `official_` prefix. They cover finished
gameweeks only. Official FPL exposes only *current* ownership, so played rows
leave `selected_by_percent` empty and record the capture-time value as
`selected_by_percent_at_capture`; `official_selected` is the point-in-time
manager count.

Snapshots, predictions, squads, and metadata for a Gameweek are written only
while that Gameweek is still open (before its deadline). Each file therefore
holds the last pre-deadline forecast, and requests for past, live, or
far-future Gameweeks never replace it.

Once a Gameweek's deadline has passed, its prediction never changes. Every
prediction endpoint (`/api/scout`, `/api/gw/scout`, `/api/gw/playerpoints`,
and team ratings) serves the archived pre-deadline forecast, with
`"frozen": true` and `forecast_captured_at`, and re-selects the squad from it
instead of predicting again with newer injuries, ownership, or fixtures. Open
Gameweeks are predicted live (`"frozen": false`). The served forecast is
stored in the Gameweek's metadata file, which is replaced in one atomic write,
so a failure part-way through a capture leaves the previous forecast intact.
It is served as frozen when the metadata names the Gameweek and was captured
before the deadline (`"freeze_method": "deadline"`).
A closed Gameweek without such a forecast, for example one that closed before
frozen forecasts shipped, is frozen on its first recall
(`"freeze_method": "after-deadline"`). If an older version's archive proves it
holds the forecast made before the deadline (captured earlier), that forecast
is frozen; older versions archived requests after deadlines too, so otherwise
the prediction made on that first recall is frozen. Either way
`forecast_captured_at` is when the frozen forecast was made, and every later
recall returns it unchanged. With the archive disabled, closed Gameweeks are
predicted live (`"frozen": false`). The archive assumes one service instance,
so a single lock orders forecast writes and frozen reads.

Serving frozen forecasts still archives official results, at most every
`data_archive.results_interval_seconds` (300), so the final Gameweek's results
are collected after its deadline. This runs within the request, because Cloud
Run throttles CPU between requests. Live files are refreshed
until 30 minutes after Official FPL marks the Gameweek `data_checked`, then
kept. Invoke `/api/scout` at least once before each deadline and after each
Gameweek is finalized (for example with Cloud Scheduler) to guarantee a
complete season even when the service otherwise receives no traffic.

FPL Data imports remain permission-pending and are guarded by explicit
acknowledgement, validation, provenance recording, and atomic writes:

```bash
uv run python -m scripts.download_fpl_data \
  --season latest \
  --acknowledge-permission-pending
```

## Project layout

| Path | Purpose |
|---|---|
| `main.py` | FastAPI application, route catalog, and web entry point |
| `src/official_fpl.py` | Official FPL client, caching, and schema mapping |
| `src/scout.py` | Inference, cold start, and squad selection |
| `src/features.py` | Shared model-inference feature contract |
| `src/fpl_data_inference.py` | Guarded optional stat enrichment |
| `src/admin.py`, `src/insights.py`, `src/observability.py` | Owner dashboard API, explanations and accuracy, in-process metrics and logs |
| `static/admin/` | Owner dashboard |
| `static/` | Responsive dashboard |
| `scripts/` | Official archive collection and guarded data import |
| `tests/` | API, data, feature, inference, and selection tests |

## Author

Built by [Islam Kassem](https://kassem.dev/), Senior AI/ML Engineer in Riyadh ([GitHub](https://github.com/elcaiseri) · [LinkedIn](https://www.linkedin.com/in/elcaiseri) · [Kaggle](https://www.kaggle.com/elcaiseri)).

## License

[MIT](LICENSE)

Questions: [iqasem4444@gmail.com](mailto:iqasem4444@gmail.com)

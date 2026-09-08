# OpenFPL Scout AI

<img src="assets/openfpl-predictive-lion-frameless-2026-512.png" alt="OpenFPL Scout AI logo" width="160">

OpenFPL predicts Fantasy Premier League player points and selects a complete
15-player squad for each Gameweek. It combines live official FPL data with a
Ridge, XGBoost, CatBoost, and MLP ensemble, then presents the result through a
responsive web dashboard and FastAPI service.

[Live app](https://openfpl.kassem.dev) ·
[API reference](https://openfpl.kassem.dev/redoc) ·
[Route catalog](https://openfpl.kassem.dev/api)

## Highlights

- Official FPL is the source of truth for players, clubs, availability,
  Gameweeks, fixtures, history, live scores, managers, leagues, and rankings.
- A four-model ensemble produces player projections from leakage-safe recent
  form, minutes, availability, ownership, and fixture context.
- Inference validates each model's feature contract, caches upstream data, and
  can continue when one model fails.
- The squad selector enforces the official positional quotas and a maximum of
  three players per club, then assigns captain and vice-captain.
- GW1 uses an explicit ownership and availability cold start when no genuine
  current-season match history exists.
- The dashboard includes Gameweek planning, deadline status, fixture context,
  pitch and table views, detailed player cards, and a manager team-rating mode.
- A public FPL team ID can be scored from 0–100 against the same AI benchmark,
  with separate starting-XI, captaincy, and availability signals.
- Optional FPL Data enrichment can fill missing historical statistics from GW2
  without replacing official values.
- An owner dashboard compares archived forecasts with official results and
  tracks model status, feature coverage, enrichment, storage, and service activity.

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

## Private owner dashboard

Open `/admin` to review the system. Set a dedicated secret in `.env` locally,
or in the deployment environment, then enter it on the dashboard:

```dotenv
OPENFPL_ADMIN_KEY=<a-long-random-owner-secret>
```

Generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Existing `VALID_API_KEYS` do not grant owner access. Without an owner key,
the dashboard data endpoint is disabled. Use HTTPS for a hosted deployment.
The browser keeps the key only in memory; locking the dashboard or reloading
clears it. Owner responses use `Cache-Control: no-store`. The login shell
contains no system data, and owner routes are omitted from the public API
catalog, OpenAPI, and sitemap.

The Observatory is a system admin workspace. No FPL manager ID or linked
personal entry is required. Seven views share season and gameweek selectors:

- **Season overview:** matched predicted/actual points, MAE, RMSE, bias,
  within-two accuracy, rank correlation, gameweek trends, a 38-week coverage
  map, and club comparisons. **All archived runs** includes retrospective
  comparisons; **Verified pre-deadline only** isolates advance forecasts.
- **Scout:** the actual saved shortlist, captain and vice-captain, with explicit
  expected points (**xPts**) vs official gameweek points (**pts**) for every
  pick and the full shortlist. Includes errors, season totals/trends,
  positional comparisons, player dossiers and a Scout-only CSV. Every pick
  counts once; season metrics compare the same matched picks with finalized
  scores and honor the evidence scope. A missing return keeps the full
  shortlist's actual total unknown. Missing or mismatched squads are excluded.
- **Gameweek centre:** official fixtures and football statistics, forecast vs
  actual player leaderboards, top-ten overlap, haul rate, NDCG, calibration,
  scatter plots, surprises, and position/club breakdowns. Club totals are FPL
  player points, not predicted match scores. Fixture scores are official only.
- **Manager decisions:** a legal XI derived from the saved 15-player shortlist,
  captain, bench, fixed-XI predicted/actual returns, and hindsight opportunity.
  This budget-free benchmark doubles the captain and applies no autosubs or
  chips. Hindsight optimizes the same complete shortlist using actual results;
  it is not an achievable forecast. The official manager average is context,
  since real entries operate under different constraints. Incomplete or
  mismatched squads do not generate a derived XI.
- **Player intelligence:** searchable, filtered, paginated comparisons, CSV
  exports with timing provenance, and player dossiers with gameweek histories
  and saved component-model outputs. Dossier metrics include all final saved
  points comparisons, with timing shown for every row.
- **Model lab:** live-season ensemble/component results, recorded holdout and
  out-of-fold model/baseline leaderboards, calibration, scatter samples, error
  trends, validation folds, and input features. Training artifacts are read
  from beside configured models (`holdout_predictions.csv`,
  `oof_predictions.csv`, `training_metadata.json`). Their season identifiers,
  populations and model versions remain distinct from live-season accuracy.
- **System health:** model loading/inference status, feature coverage,
  enrichment, archive ledger, an explicit upcoming forecast capture action,
  and service telemetry. Telemetry covers this process since startup; latency
  uses its latest 200 non-dashboard requests.

GW1 ownership cold-start scores are ranking inputs, not predicted points.
They are evaluated through ranking quality and realized squad returns, and
excluded from MAE/RMSE, calibration, and matched points totals. Unknown values
stay missing; they are never substituted with zero.

**All actual pts** includes finalized returns from GW1 onward, including
ownership-ranking runs, in season, Scout, player, club and position totals.
Player leaders are ordered by these full available returns. **Matched xPts/pts**
compares only rows with both a real points forecast and an official result.
Scout gameweek charts show full-shortlist totals, including GW1's actual points;
missing xPts or incomplete actual totals remain gaps. Both views honor the
selected evidence scope.

A preserved retrospective snapshot may include `snapshot_kind`,
`snapshot_created_at_utc`, and a season/gameweek-bound `actuals_snapshot` of
final official results. The original forecast capture timestamp is retained.
Saving such a snapshot after the deadline never grants pre-deadline eligibility.
Its final results can still be read if the separate live-score cache is missing.

Successful scout runs now save an atomic forecast bundle at
`data/archive/<season>/evaluation/gw_XX.json`. It can be refreshed before the
official deadline, and is preserved after that deadline. The matching squad is
attached only while the same forecast is still current and before the deadline.
New captures also save raw individual model outputs and player context in the
bundle and in `diagnostics/gw_XX.json`. Legacy runs retain their original
evidence: missing model outputs are not reconstructed after results are known.
Legacy archives remain visible, with capture timestamps checked for eligibility.
Unknown or late forecasts populate the archive comparison cards and trend, with
their timing clearly labeled. They are excluded from verified pre-deadline
accuracy; rerunning a past gameweek cannot create a verified historical forecast.
The gameweek review opens on the latest archived gameweek with matched scores;
upcoming forecasts can still be selected explicitly.

Scores are joined by official player ID within the same season and gameweek.
Missing scores stay missing, and event-live totals already include double
gameweeks. Results remain provisional until official FPL reports both `finished`
and `data_checked`. The first finalized score fetch bypasses the live cache and
is stored under `evaluation/actuals/`. Old live files without finalization
provenance remain provisional. Upstream failures retain saved scores with a
visible warning. The dashboard refreshes every 60 seconds while visible; current
official data uses the shared FPL cache. Reading or refreshing views never runs
inference. **Capture forecast** explicitly runs inference for an upcoming
official deadline and records the real capture time.

To build a complete record, schedule `/api/scout` before each deadline and open
the dashboard (or call `GET /api/admin/dashboard` with
`Authorization: Bearer <owner-key>`) after results finalize. Continue mounting
the data directory read-write. No sample results are used in the dashboard.
The same owner authorization protects `GET /api/admin/players/{player_id}`,
`GET /api/admin/models?dataset=holdout|cross-validation`, and
`POST /api/admin/capture?gameweek=4`. Capture rejects expired deadlines.

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
512 MiB, concurrency 4, scale-to-zero, and a three-instance cost cap:

```bash
gcloud run services update SERVICE \
  --region REGION \
  --cpu 1 \
  --memory 512Mi \
  --concurrency 4 \
  --min 0 \
  --max 3 \
  --cpu-throttling \
  --cpu-boost
```

Increase memory only if Cloud Monitoring reports pressure or out-of-memory
restarts. Use a minimum instance only when lower cold-start latency is worth
the idle charge.

## API

The web application and its supporting read endpoints are public. Administrative
and extended data routes require `Authorization: Bearer <token>`.

```bash
curl "https://openfpl.kassem.dev/api/scout?gameweek=1"
curl "https://openfpl.kassem.dev/api/scout/team-rating?entry_id=1234567&gameweek=1"
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

## Models and data

Runtime data flows from official FPL through the shared feature pipeline, model
ensemble, and squad selector. The optional enrichment layer accepts only the
configured season, fills missing values only, rejects stale or poorly matched
data, and falls back to official-only inference on failure.

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
gameweek-history field with an `official_` prefix. Completed live files are
immutable; the current gameweek, upcoming predictions, and snapshots are
refreshed as new requests arrive. Invoke `/api/scout` at least once after each
gameweek is finalized (for example with Cloud Scheduler) to guarantee a
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
| `static/` | Responsive dashboard |
| `scripts/` | Official archive collection and guarded data import |
| `tests/` | API, data, feature, inference, and selection tests |

## License

[MIT](LICENSE)

Questions: [iqasem4444@gmail.com](mailto:iqasem4444@gmail.com)

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

Squad selection is intentionally budget-free. Prices are returned for context
but do not affect player projections or selection.

## Run locally

Requirements: Python 3.9 or newer and
[uv](https://docs.astral.sh/uv/). Model artifacts must exist at the paths in
`config/config.yaml`; generated models are not stored in Git.

```bash
uv sync --all-groups
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
    │   └── player-stats/gw_02.csv    # one training table per played GW
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

Archive active-season official history for future training:

```bash
uv run python -m scripts.collect_official_fpl --gameweek 39
```

Train all four pipelines with chronological cross-validation and an untouched
latest-season holdout:

```bash
uv run --group train python trainer-booster.py \
  --data-dir data/official \
  --output-dir models \
  --folds 5 \
  --tune
```

Use `--quick` for a training smoke test. Runs write model pipelines, fold and
holdout metrics, predictions, ensemble weights, metadata, and training history
to the selected output directory.

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
| `src/features.py` | Shared training and runtime feature contract |
| `src/fpl_data_inference.py` | Guarded optional stat enrichment |
| `static/` | Responsive dashboard |
| `trainer-booster.py` | Time-aware model training and evaluation |
| `scripts/` | Official archive collection and guarded data import |
| `tests/` | API, data, feature, inference, and selection tests |

## License

[MIT](LICENSE)

Questions: [iqasem4444@gmail.com](mailto:iqasem4444@gmail.com)

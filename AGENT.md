# Working on OpenFPL Scout AI

Start with the component that owns the behaviour you are changing.

- For API routes, start in `main.py`. Check [Docs.md](Docs.md) when changing authentication, request parameters, or response fields.
- For player projections, start in `src/scout.py`. It uses the model inputs defined in `src/features.py` and the official FPL data client in `src/official_fpl.py`.
- For dashboard changes, edit the HTML, CSS, and JavaScript in `static/`. The dashboard loading tests live in `tests/frontend_loading.test.cjs`.

## Running locally

Install the locked dependencies with `uv sync --locked`, then start the app with `uv run uvicorn main:app --reload`. Local predictions need the model files at the paths configured in [config/config.yaml](config/config.yaml). See the [README](README.md) for model setup and environment variables.

## Running the checks

From the repository root, run the same Python and Node.js checks as CI.

```sh
uv run python -m unittest discover -s tests
node --test tests/frontend_loading.test.cjs
```

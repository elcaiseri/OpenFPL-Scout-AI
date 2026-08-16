FROM python:3.9-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --all-groups --no-cache --no-install-project

FROM python:3.9-slim-trixie AS runtime

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    OPENFPL_ENV="production" \
    PORT="8000" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    OMP_NUM_THREADS="1" \
    OPENBLAS_NUM_THREADS="1" \
    MKL_NUM_THREADS="1" \
    NUMEXPR_NUM_THREADS="1"

COPY --from=builder /app/.venv /app/.venv
COPY . ./

RUN mkdir -p /app/data /app/models

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"${PORT}\""]

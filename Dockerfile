FROM python:3.9-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    OPENFPL_ENV="production" \
    PORT="8000" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --all-groups --no-cache

COPY . .
RUN mkdir -p /app/data /app/models

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"${PORT}\""]

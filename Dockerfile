FROM python:3.9-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Bring in uv for dependency management and .venv creation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# Allow optional dependency groups (e.g. --group train) while keeping runtime deps by default
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --all-groups

# Copy application code last to maximise layer caching
COPY . .
RUN mkdir -p data/internal/scout_team

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

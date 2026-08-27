FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY backend/pyproject.toml backend/uv.lock backend/README.md backend/alembic.ini ./
COPY backend/src ./src
RUN uv sync --frozen --no-dev --group analysis

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY backend/src ./src
RUN useradd --system --uid 10001 analysis
USER analysis
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["dramatiq", "cti_app.workers.analysis_tasks"]

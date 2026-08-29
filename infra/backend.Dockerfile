FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends --yes pandoc \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml backend/uv.lock backend/README.md backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/src ./src
COPY backend/assets ./assets
RUN uv sync --frozen --no-dev --no-group analysis

ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "cti_app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

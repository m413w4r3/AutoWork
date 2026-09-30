FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl pandoc \
    && rm -rf /var/lib/apt/lists/*

COPY infra/d2.lock /usr/local/share/autowork/d2.lock
COPY scripts/install-d2.sh /usr/local/bin/install-d2
RUN D2_LOCK_FILE=/usr/local/share/autowork/d2.lock \
    TARGETARCH="$TARGETARCH" /usr/local/bin/install-d2 /usr/local/bin

COPY backend/pyproject.toml backend/uv.lock backend/README.md backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/src ./src
COPY backend/assets ./assets
RUN uv sync --frozen --no-dev --no-group analysis

ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "cti_app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

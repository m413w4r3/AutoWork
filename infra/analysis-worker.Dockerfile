FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/mandiant/capa-rules.git /tmp/capa-rules \
    && cd /tmp/capa-rules \
    && git checkout --detach 2af9fbfc1c9b4634dbeb76b5d34fca9389fa7f80 \
    && rm -rf .git \
    && mkdir -p /app/rules/capa \
    && cp -a /tmp/capa-rules/. /app/rules/capa/ \
    && chmod -R a-w /app/rules/capa \
    && rm -rf /tmp/capa-rules
COPY backend/pyproject.toml backend/uv.lock backend/README.md backend/alembic.ini ./
COPY backend/src ./src
RUN uv sync --frozen --no-dev --group analysis

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY backend/src ./src
COPY backend/tools ./tools
COPY --from=builder --chown=root:root /app/rules/capa /app/rules/capa
RUN chmod -R a-w /app/rules/capa
RUN useradd --system --uid 10001 analysis
USER analysis
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["dramatiq", "cti_app.workers.analysis_tasks"]

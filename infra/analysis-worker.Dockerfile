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
    && printf '%s\n' '2af9fbfc1c9b4634dbeb76b5d34fca9389fa7f80' > /app/rules/capa/AUTOWORK_RULESET_REVISION \
    && rm -rf /tmp/capa-rules
COPY backend/pyproject.toml backend/uv.lock backend/README.md backend/alembic.ini ./
COPY backend/src ./src
RUN uv sync --frozen --no-dev --group analysis
RUN git clone --depth 1 --branch v9.4.0 https://github.com/mandiant/capa.git /tmp/capa-source \
    && mkdir -p /app/.venv/lib/python3.12/site-packages/sigs \
    && cp -a /tmp/capa-source/sigs/. /app/.venv/lib/python3.12/site-packages/sigs/ \
    && rm -rf /tmp/capa-source
RUN PYTHONPATH=/app/src /app/.venv/bin/python -c \
    "from pathlib import Path; from cti_app.infrastructure.capa import ruleset_manifest; path=Path('/app/rules/capa'); (path/'AUTOWORK_RULESET_SHA256').write_text(ruleset_manifest(path) or '', encoding='ascii')" \
    && chmod -R a-w /app/rules/capa

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY backend/src ./src
COPY backend/tools ./tools
COPY --from=builder --chown=root:root /app/rules/capa /app/rules/capa
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends tshark \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a-w /app/rules/capa \
    && tshark --version \
    && capinfos -v
RUN useradd --system --uid 10001 analysis
USER analysis
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANALYSIS_WORKER_ISOLATION_PROFILE=bookworm-internal-v1
CMD ["dramatiq", "cti_app.workers.analysis_tasks"]

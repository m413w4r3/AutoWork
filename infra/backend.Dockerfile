FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY infra/d2.lock /usr/local/share/autowork/d2.lock
COPY scripts/install-d2.sh /usr/local/bin/install-d2
RUN D2_LOCK_FILE=/usr/local/share/autowork/d2.lock \
    TARGETARCH="$TARGETARCH" /usr/local/bin/install-d2 /usr/local/bin

COPY infra/typst.lock /usr/local/share/autowork/typst.lock
COPY scripts/install-typst.sh /usr/local/bin/install-typst
RUN TYPST_LOCK_FILE=/usr/local/share/autowork/typst.lock \
    TARGETARCH="$TARGETARCH" /usr/local/bin/install-typst /usr/local/bin
RUN typst --version && ! command -v pandoc

COPY infra/typst-fonts.lock /usr/local/share/autowork/typst-fonts.lock
COPY infra/typst-fonts-sources.lock /usr/local/share/autowork/typst-fonts-sources.lock
COPY scripts/install-typst-fonts.sh /usr/local/bin/install-typst-fonts
RUN TYPST_FONTS_LOCK_FILE=/usr/local/share/autowork/typst-fonts.lock \
    TYPST_FONTS_SOURCE_LOCK_FILE=/usr/local/share/autowork/typst-fonts-sources.lock \
    /usr/local/bin/install-typst-fonts /usr/local/share/autowork/typst-fonts

COPY chpTypst /app/chpTypst
COPY scripts/typst_edition_smoke.py /app/scripts/typst_edition_smoke.py

COPY backend/pyproject.toml backend/uv.lock backend/README.md backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/src ./src
RUN uv sync --frozen --no-dev --no-group analysis

ENV PATH="/app/.venv/bin:$PATH" \
    FONT_BUNDLE_ROOT=/usr/local/share/autowork/typst-fonts
CMD ["uvicorn", "cti_app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

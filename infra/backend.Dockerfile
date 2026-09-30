FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG D2_VERSION=0.9.0
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl pandoc \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "$TARGETARCH" in \
        amd64) d2_arch=amd64; d2_sha256=5669ddc46b99e942cc96078f4a4e36d5e62103348f4c05179ede27802fdd87a9 ;; \
        arm64) d2_arch=arm64; d2_sha256=ac2c028697199479acb321db1e3d68caee9f2ba492ed73caa3cd13f3829bf913 ;; \
        *) echo "unsupported D2 architecture: ${TARGETARCH:-unset}" >&2; exit 1 ;; \
    esac; \
    build_dir="$(mktemp -d)"; \
    archive="${build_dir}/d2.tar.gz"; \
    curl --fail --location --silent --show-error \
        "https://github.com/terrastruct/d2/releases/download/v${D2_VERSION}/d2-v${D2_VERSION}-linux-${d2_arch}.tar.gz" \
        --output "$archive"; \
    echo "${d2_sha256}  ${archive}" | sha256sum --check --status; \
    mkdir "${build_dir}/extract"; \
    tar -xzf "$archive" --strip-components=1 -C "${build_dir}/extract"; \
    install -m 0755 "${build_dir}/extract/bin/d2" /usr/local/bin/d2; \
    rm -rf "$build_dir"; \
    test "$(d2 --version)" = "v${D2_VERSION}"

COPY backend/pyproject.toml backend/uv.lock backend/README.md backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/src ./src
COPY backend/assets ./assets
RUN uv sync --frozen --no-dev --no-group analysis

ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "cti_app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

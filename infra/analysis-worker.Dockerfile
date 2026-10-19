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
COPY infra/install_ilspycmd.py /app/tools/install_ilspycmd.py
COPY --from=builder --chown=root:root /app/rules/capa /app/rules/capa
# Suricata is not in bookworm main; the pinned 7.0.x build comes from bookworm-backports.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        ca-certificates curl \
    && curl --fail --silent --show-error --location \
        https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
        --output /tmp/packages-microsoft-prod.deb \
    && echo "8434dcb8c346dc95fbd63dbece056c343704590b58b6a5c323d39acf52bf0b48  /tmp/packages-microsoft-prod.deb" \
        | sha256sum --check --status \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && echo "deb http://deb.debian.org/debian bookworm-backports main" \
        > /etc/apt/sources.list.d/bookworm-backports.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        tshark file binutils xxd coreutils mono-utils=6.8.0.105+dfsg-3.3+deb12u1 \
        dotnet-runtime-8.0=8.0.32-1 \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        -t bookworm-backports suricata=1:7.0.10-1~bpo12+1 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /tmp/packages-microsoft-prod.deb \
    && dotnet --list-runtimes | grep -q 'Microsoft.NETCore.App 8.0.32 ' \
    && PYTHONPATH=/app/src python /app/tools/install_ilspycmd.py \
    && ilspycmd --disable-updatecheck --version \
    && chmod -R a-w /app/rules/capa \
    && file --version \
    && strings --version \
    && readelf --version \
    && objdump --version \
    && xxd -v \
    && sha256sum --version \
    && dpkg-query -W -f='${Version}\n' mono-utils \
    && (monodis --help 2>&1 | grep -q -- "--userstrings") \
    && tshark --version \
    && capinfos -v \
    && mkdir -p /app/rules/suricata \
    && printf '%s' 'alert tcp any any -> any any (msg:"AUTOWORK benign replay marker"; content:"AUTOWORK_SURICATA_BENIGN"; sid:9900001; rev:1;)' > /app/rules/suricata/autowork.rules \
    && printf '%s\n' 'autowork-suricata-v1' > /app/rules/suricata/AUTOWORK_RULESET_REVISION \
    && printf '%s\n' '66f69f0cf586a87f739d4f63c6bb9d23a7d303f8adf23c2773a31839ea7f9f59' > /app/rules/suricata/AUTOWORK_RULESET_SHA256 \
    && chmod -R a-w /app/rules/suricata \
    && suricata -V
RUN useradd --system --uid 10001 analysis
USER analysis
ENV PATH="/app/.venv/bin:$PATH" \
    DOTNET_ROOT=/usr/share/dotnet \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    DOTNET_NOLOGO=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANALYSIS_WORKER_ISOLATION_PROFILE=bookworm-internal-v1
CMD ["dramatiq", "cti_app.workers.analysis_tasks"]

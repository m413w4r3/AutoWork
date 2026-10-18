FROM eclipse-temurin:21.0.12.1_1-jdk-jammy

ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:${PATH} \
    GHIDRA_INSTALL_DIR=/opt/ghidra \
    GHIDRA_EXPECTED_VERSION=12.1.4 \
    GHIDRA_EXPECTED_JAVA_VERSION=21.0.12.1 \
    PYTHONPATH=/opt/autowork \
    HOME=/work/home \
    XDG_CONFIG_HOME=/work/home/.config \
    XDG_CACHE_HOME=/work/home/.cache \
    TMPDIR=/work/home/tmp \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libfontconfig1 libfreetype6 python3 python-is-python3 unzip \
    && rm -rf /var/lib/apt/lists/*

COPY infra/ghidra.lock /tmp/ghidra.lock
RUN set -eu; \
    . /tmp/ghidra.lock; \
    archive="/tmp/${GHIDRA_ARCHIVE_NAME}"; \
    curl --fail --location --silent --show-error "${GHIDRA_ARCHIVE_URL}" --output "${archive}"; \
    printf '%s  %s\n' "${GHIDRA_SHA256}" "${archive}" | sha256sum --check --status; \
    unzip -q "${archive}" -d /opt; \
    mv "/opt/ghidra_${GHIDRA_VERSION}_PUBLIC" /opt/ghidra; \
    grep -Fx "application.version=${GHIDRA_VERSION}" /opt/ghidra/Ghidra/application.properties; \
    rm -f "${archive}" /tmp/ghidra.lock

RUN useradd --uid 10002 --create-home --home-dir /home/ghidra --shell /usr/sbin/nologin ghidra \
    && mkdir -p /work /opt/autowork \
    && chown 10002:10002 /work

COPY backend/src/cti_app /opt/autowork/cti_app
WORKDIR /opt/autowork
USER 10002:10002

ENTRYPOINT ["python3", "-m", "cti_app.ghidra_service.server"]

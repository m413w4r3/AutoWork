"""Contract checks on how the bridge is deployed and launched.

Pins `compose.yaml`/`Makefile`/`tools/status.py`/`server.py` invariants that
protect the persistent browser profile (`bridge_data`) and the worker/bridge
timeout ordering. Nothing here imports `bridge.*`: these are text-content
assertions on the repo's deployment files.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_compose_and_makefile_bridge_lifecycle_contract() -> None:
    root = Path(__file__).parents[2]
    compose = (root / "compose.yaml").read_text()
    makefile = (root / "Makefile").read_text()
    environment = compose.split("environment: &backend-environment", 1)[1].split(
        "\n    volumes:", 1
    )[0]
    backend = compose.split("\n  backend:", 1)[1].split("\n  migrate:", 1)[0]
    backend_depends = backend.split("\n    depends_on:", 1)[1].split("\n    healthcheck:", 1)[0]
    worker = compose.split("\n  worker:", 1)[1].split("\n  job-recovery:", 1)[0]
    postgres = compose.split("\n  postgres:", 1)[1].split("\n  redis:", 1)[0]
    redis = compose.split("\n  redis:", 1)[1].split("\n  minio:", 1)[0]

    assert (
        "OPENAI_BRIDGE_BASE_URL: "
        "${OPENAI_BRIDGE_BASE_URL:-http://chatgpt-bridge:8001/v1}" in environment
    )
    assert "127.0.0.1:8001/v1" not in environment
    assert "chatgpt-bridge:\n        condition: service_healthy" in worker
    assert "chatgpt-bridge:" not in backend_depends
    assert "depends_on:" not in postgres
    assert "depends_on:" not in redis
    assert "bridge_data:/data" in compose
    assert "stop_grace_period: 30s" in compose

    assert "$(COMPOSE) up -d --build --wait" in makefile
    assert "$(COMPOSE) down -v" not in makefile
    # A data reset must never drop bridge_data: it holds the authenticated
    # ChatGPT browser profile, and losing it means logging the bridge back in.
    assert "docker volume rm" in makefile
    assert "bridge_data" not in makefile.split("CLEAN_VOLUMES = ", 1)[1].split("\n", 1)[0]
    assert "python tools/status.py" in makefile
    status_script = (root / "chatgpt-bridge" / "tools" / "status.py").read_text()
    assert 'os.getenv("BRIDGE_API_KEY")' in status_script
    assert "print(key)" not in status_script
    server = (root / "chatgpt-bridge" / "server.py").read_text()
    assert "access_log=False" in server
    assert 'log_level="warning"' in server
    assert "logger.propagate = False" in server


def test_worker_time_limit_outlives_the_bridge_total_timeout() -> None:
    """Le worker doit survivre au bridge, jamais l'inverse.

    Les deux bornes vivent dans deux composants différents ; les laisser dériver
    l'une par rapport à l'autre a déjà tué le worker en pleine attente. Le défaut
    du bridge fut aussi longtemps recopié depuis MODEL_REQUEST_TIMEOUT_SECONDS,
    qui borne un appel HTTP court et n'a rien à dire sur la durée d'une recherche.
    """
    compose = (Path(__file__).parents[2] / "compose.yaml").read_text()

    def default_of(variable: str) -> float:
        match = re.search(rf"\$\{{{variable}:-([0-9.]+)\}}", compose)
        assert match, f"{variable} n'a plus de défaut dans compose.yaml"
        return float(match.group(1))

    bridge_total = default_of("BRIDGE_TOTAL_TIMEOUT_SECONDS")
    actor_limit = default_of("JOB_ACTOR_TIME_LIMIT_SECONDS")

    # Une recherche approfondie ChatGPT doit pouvoir occuper l'heure entière
    # sans être coupée, et le worker doit lui survivre avec de la marge.
    assert bridge_total >= 3600
    assert actor_limit >= bridge_total + 600
    assert "BRIDGE_TOTAL_TIMEOUT: ${MODEL_REQUEST_TIMEOUT_SECONDS" not in compose

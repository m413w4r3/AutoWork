"""Contract checks for the standalone Bridge deployment."""

from __future__ import annotations

import re
from pathlib import Path


def test_compose_and_launch_contract() -> None:
    root = Path(__file__).parents[1]
    compose = (root / "compose.yaml").read_text()
    makefile = (root / "Makefile").read_text()
    server = (root / "server.py").read_text()
    status_script = (root / "tools" / "status.py").read_text()

    assert "chatgpt-bridge:" in compose
    assert "bridge_data:/data" in compose
    assert "BRIDGE_RUN_DB: /data/bridge-runs.sqlite3" in compose
    assert "stop_grace_period: 30s" in compose
    assert "${BRIDGE_BIND_ADDRESS:-127.0.0.1}" in compose

    assert "postgres:" not in compose
    assert "redis:" not in compose
    assert "minio:" not in compose
    assert "backend:" not in compose
    assert "worker:" not in compose

    assert "python tools/status.py" in makefile

    assert 'os.getenv("BRIDGE_API_KEY")' in status_script
    assert "print(key)" not in status_script

    assert "access_log=False" in server
    assert 'log_level="warning"' in server
    assert "logger.propagate = False" in server


def test_default_total_timeout_allows_long_research() -> None:
    root = Path(__file__).parents[1]
    compose = (root / "compose.yaml").read_text()

    match = re.search(
        r"\$\{BRIDGE_TOTAL_TIMEOUT:-([0-9.]+)\}",
        compose,
    )

    assert match
    assert float(match.group(1)) >= 3600

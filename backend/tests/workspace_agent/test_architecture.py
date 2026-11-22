"""Static oracles: the workspace agent is stdlib-only, AF_UNIX-only, side-effect free."""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
from pathlib import Path

from tests.workspace_agent.conftest import REPOSITORY_ROOT, agent_environment

PACKAGE_DIR = REPOSITORY_ROOT / "sidecars" / "workspace_agent"

# Explicit allowlist: every import in the sidecar must come from this set. Any
# addition is a deliberate review point (no httpx, requests, pydantic, watcher).
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "argparse",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "fcntl",
        "grp",
        "hashlib",
        "hmac",
        "http",
        "json",
        "logging",
        "os",
        "pathlib",
        "re",
        "secrets",
        "signal",
        "socket",
        "stat",
        "sys",
        "threading",
        "time",
        "typing",
        "unicodedata",
        "urllib",
        "uuid",
    }
)


def _package_files() -> list[Path]:
    files = sorted(PACKAGE_DIR.glob("*.py"))
    assert files, "workspace agent package modules are missing"
    return files


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # relative import inside sidecars.workspace_agent
            assert node.module is not None
            roots.add(node.module.split(".")[0])
    return roots


def test_agent_imports_only_allowlisted_stdlib_modules() -> None:
    offenders = {
        path.name: sorted(_top_level_imports(path) - ALLOWED_IMPORTS)
        for path in _package_files()
        if _top_level_imports(path) - ALLOWED_IMPORTS
    }
    assert offenders == {}


def test_agent_never_reaches_for_inet_sockets_or_outbound_connections() -> None:
    for path in _package_files():
        source = path.read_text(encoding="utf-8")
        assert "AF_INET" not in source, path.name
        assert "AF_INET6" not in source, path.name
        assert "create_connection" not in source, path.name
        assert ".connect(" not in source, path.name


def test_http_server_binds_af_unix_only() -> None:
    from sidecars.workspace_agent.http_server import WorkspaceAgentHTTPServer

    assert WorkspaceAgentHTTPServer.address_family == socket.AF_UNIX
    assert "AF_INET" not in repr(WorkspaceAgentHTTPServer.address_family)


def test_importing_the_entrypoint_has_no_startup_side_effects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    os.chmod(runtime_dir, 0o700)
    env = agent_environment()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    completed = subprocess.run(
        [sys.executable, "-c", "import sidecars.workspace_agent.__main__"],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (home / "work").exists()
    assert not any(runtime_dir.iterdir())

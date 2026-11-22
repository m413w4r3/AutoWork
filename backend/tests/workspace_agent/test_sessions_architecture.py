"""Static oracles for the H1 session supervisor package (stdlib, AF_UNIX only).

The existing workspace-agent architecture test globs the package's top-level
modules; this companion oracle covers ``sidecars/workspace_agent/sessions/``
without widening the W1 allowlist. ``fake_cli.py`` is the only file allowed to
call ``connect``: it stands in for the MCP shim and must reach the per-session
AF_UNIX secret socket, never a network address.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.workspace_agent.conftest import REPOSITORY_ROOT

SESSIONS_PACKAGE = REPOSITORY_ROOT / "sidecars" / "workspace_agent" / "sessions"

ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "argparse",
        "base64",
        "binascii",
        "collections",
        "ctypes",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "logging",
        "os",
        "pathlib",
        "re",
        "shutil",
        "signal",
        "socket",
        "stat",
        "struct",
        "subprocess",
        "sys",
        "threading",
        "time",
        "typing",
        "uuid",
    }
)


def _session_modules() -> list[Path]:
    modules = sorted(SESSIONS_PACKAGE.glob("*.py"))
    assert modules, "session supervisor modules are missing"
    return modules


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            assert node.module is not None
            roots.add(node.module.split(".")[0])
    return roots


def test_sessions_package_imports_only_allowlisted_stdlib_modules() -> None:
    offenders = {
        path.name: sorted(_top_level_imports(path) - ALLOWED_IMPORTS)
        for path in _session_modules()
        if _top_level_imports(path) - ALLOWED_IMPORTS
    }
    assert offenders == {}


def test_sessions_package_never_uses_inet_sockets() -> None:
    for path in _session_modules():
        source = path.read_text(encoding="utf-8")
        assert "AF_INET" not in source, path.name
        assert "AF_INET6" not in source, path.name
        assert "create_connection" not in source, path.name
        if path.name != "fake_cli.py":
            assert ".connect(" not in source, path.name


def test_fake_cli_connects_only_to_the_session_secret_socket() -> None:
    source = (SESSIONS_PACKAGE / "fake_cli.py").read_text(encoding="utf-8")
    assert "AF_UNIX" in source
    assert "AUTOWORK_SESSION_SOCKET" in source
    assert "from sidecars" not in source
    assert "import sidecars" not in source

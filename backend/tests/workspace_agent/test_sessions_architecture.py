"""Static oracles for the H1/H2 session package (stdlib, AF_UNIX and loopback).

The existing workspace-agent architecture test globs the package's top-level
modules; this companion oracle covers ``sidecars/workspace_agent/sessions/``
without widening the W1 allowlist. ``fake_cli.py`` and ``shim.py`` are allowed
to call ``connect``: the fake CLI receives the per-session AF_UNIX secret, and
the shim receives the same secret then signs fixed loopback HTTP requests (H2).
``entrypoint.py`` is the trusted launch wrapper that runs only inside the
bwrap ``--unshare-net`` namespace: it owns the ``127.0.0.1:3128`` provider
forwarder (loopback must be usable, no other route exists) and probes
unroutable destinations.  No other sessions module may name an INET socket
family.
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
        "hmac",
        "http",
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
        "urllib",
        "uuid",
    }
)

# ``shim.py`` falls back to a bare sibling import when the supervisor copies
# it as a standalone script into the session config (it is not a package then).
LOCAL_MODULES = frozenset({"catalog"})


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
        path.name: sorted(_top_level_imports(path) - ALLOWED_IMPORTS - LOCAL_MODULES)
        for path in _session_modules()
        if _top_level_imports(path) - ALLOWED_IMPORTS - LOCAL_MODULES
    }
    assert offenders == {}


# entrypoint.py is the only file that may name an INET socket family: it runs
# exclusively inside the route-less bwrap network namespace, where it must own
# the loopback provider listener and probe unreachable destinations.  The list
# is closed; adding a file here is a deliberate review point.
INET_SOCKET_ALLOWED = frozenset({"entrypoint.py"})


def test_sessions_package_never_uses_inet_sockets() -> None:
    for path in _session_modules():
        if path.name in INET_SOCKET_ALLOWED:
            continue
        source = path.read_text(encoding="utf-8")
        assert "AF_INET" not in source, path.name
        assert "AF_INET6" not in source, path.name
        assert "create_connection" not in source, path.name
        if path.name not in {"fake_cli.py", "shim.py"}:
            assert ".connect(" not in source, path.name


def test_fake_cli_connects_only_to_the_session_secret_socket() -> None:
    source = (SESSIONS_PACKAGE / "fake_cli.py").read_text(encoding="utf-8")
    assert "AF_UNIX" in source
    assert "AUTOWORK_SESSION_SOCKET" in source
    assert "from sidecars" not in source
    assert "import sidecars" not in source


def test_shim_connects_only_to_the_secret_socket_and_loopback_http() -> None:
    source = (SESSIONS_PACKAGE / "shim.py").read_text(encoding="utf-8")
    assert "AF_UNIX" in source
    assert "AUTOWORK_SESSION_SOCKET" in source
    assert "import http.client" in source
    assert "http.client.HTTPConnection" in source
    assert "AUTOWORK_BACKEND_URL" in source
    assert 'LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})' in source
    assert "localhost" not in source.lower()
    assert "from sidecars" not in source
    assert "import sidecars" not in source

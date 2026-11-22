"""M24-05 H2 secret oracles: one-shot handoff, fail-closed, never leaked.

The shim reads the ADR-0011 D7 session secret once from the H1
``ipc/shim.sock`` frame and keeps it in memory only. It must never appear in
argv, the environment, stdout, stderr or a tool result, and a missing or
invalid handoff must stop the process before it serves a single tool.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

from sidecars.workspace_agent.sessions import shim

from tests.workspace_agent import session_shim_support as support


def test_secret_never_in_argv_env_or_log() -> None:
    with support.ShimHarness(backend_url="http://127.0.0.1:1") as shim_process:
        initialized = shim_process.initialize()
        assert initialized["result"]["protocolVersion"]
        listed = shim_process.request("tools/list")
        assert "result" in listed
        assert shim_process.broker is not None
        support.wait_until(
            lambda: shim_process.broker is not None and shim_process.broker.delivered
        )
        cmdline, environ = support.proc_identity(shim_process.process.pid)
        assert support.SECRET_HEX not in cmdline
        assert support.SECRET_B64 not in cmdline
        if environ is not None:
            assert support.SECRET_HEX not in environ
            assert support.SECRET_B64 not in environ
            assert not any("secret" in line.lower() for line in environ.split("\0") if "=" in line)
        # The exact launch environment is closed and carries no secret; on
        # Linux the shim additionally seals /proc/<pid>/environ (dumpable=0).
        launch = shim_process.environment
        assert support.SECRET_HEX not in json.dumps(launch)
        assert support.SECRET_B64 not in json.dumps(launch)
        assert not any("secret" in key.lower() for key in launch)
        # The handoff socket path is configuration, the secret itself is not.
        assert "AUTOWORK_SESSION_SOCKET" in launch
        assert "AUTOWORK_SESSION_ID" in launch
        exit_code = shim_process.close()
        assert exit_code == 0
        output = shim_process.stdout_text() + shim_process.stderr_text()
        assert support.SECRET_HEX not in output
        assert support.SECRET_B64 not in output
        assert support.SECRET.hex()[:16] not in output


def test_shim_refuses_to_start_without_a_handoff() -> None:
    with support.ShimHarness(start_broker=False) as shim_process:
        exit_code = shim_process.close()
    assert exit_code != 0
    stderr = shim_process.stderr_text() if shim_process else ""
    assert "session_config_missing" in stderr or "secret" in stderr
    assert support.SECRET_HEX not in stderr


def test_shim_refuses_a_socket_that_delivers_nothing() -> None:
    base = Path(tempfile.mkdtemp(prefix="m24h2-empty-", dir="/tmp"))
    path = support.free_uds_path(base)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def _close_immediately() -> None:
        connection, _ = listener.accept()
        connection.close()

    thread = threading.Thread(target=_close_immediately, daemon=True)
    thread.start()
    environment = support.shim_environment(
        base=base,
        socket_path=path,
        backend_url="http://127.0.0.1:1",
    )
    try:
        completed = support.run_shim_once(environment, timeout=10.0)
    finally:
        listener.close()
        thread.join(timeout=2)
        shutil.rmtree(base, ignore_errors=True)
    assert completed.returncode != 0
    assert b"secret" in completed.stderr.lower()
    assert support.SECRET_HEX.encode() not in completed.stderr


def test_handoff_frame_schema_and_session_are_checked() -> None:
    base = Path(tempfile.mkdtemp(prefix="m24h2-bad-", dir="/tmp"))
    path = support.free_uds_path(base)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    frame = (
        json.dumps(
            {
                "schema": "autowork-session-secret.v1",
                "session_id": support.SECOND_SESSION_ID,
                "secret": support.SECRET_B64,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    def _wrong_session() -> None:
        connection, _ = listener.accept()
        connection.sendall(frame)
        connection.close()

    thread = threading.Thread(target=_wrong_session, daemon=True)
    thread.start()
    environment = support.shim_environment(
        base=base, socket_path=path, backend_url="http://127.0.0.1:1"
    )
    try:
        completed = support.run_shim_once(environment, timeout=10.0)
    finally:
        listener.close()
        thread.join(timeout=2)
        shutil.rmtree(base, ignore_errors=True)
    assert completed.returncode != 0
    assert b"secret" in completed.stderr.lower()
    assert support.SECRET_HEX.encode() not in completed.stderr
    assert support.SECRET_B64.encode() not in completed.stderr


def test_secret_reference_signing_never_emits_the_secret() -> None:
    signature = shim.sign_request(
        support.SECRET,
        method="POST",
        path="/api/investigations/x",
        session_id=support.SESSION_ID,
        body=b"{}",
        idempotency_key="k" * 64,
        timestamp=support.FIXED_TIMESTAMP,
    )
    assert signature.startswith("v1=")
    assert len(signature) == 67
    assert support.SECRET_HEX not in signature
    assert support.SECRET_B64 not in signature

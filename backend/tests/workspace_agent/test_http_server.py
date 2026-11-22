"""HTTP/AF_UNIX behaviour of the workspace agent skeleton (ticket W1)."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest
from sidecars.workspace_agent import protocol
from sidecars.workspace_agent.agent import (
    SOCKET_FILE_NAME,
    AgentConfig,
    RuntimeDirectoryError,
    WorkspaceAgent,
)
from sidecars.workspace_agent.auth import (
    TOKEN_FILE_NAME,
    TokenFileError,
    generate_token,
    read_token_file,
)
from sidecars.workspace_agent.http_server import SocketPathError

from tests.workspace_agent.conftest import (
    REPOSITORY_ROOT,
    RunningAgent,
    agent_argv,
    agent_environment,
    raw_http_request,
    response_json,
    response_status,
    wait_for_health,
)

VALID_WORKSPACE_ID = "ws-" + "a" * 32


def test_health_is_unauthenticated_and_leaks_no_host_path(
    uds_client, agent_server: RunningAgent
) -> None:
    response = uds_client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert set(payload) == {"status", "protocol", "instance_id", "pid", "started_at"}
    assert payload["status"] == "ready"
    assert payload["protocol"] == "workspace-agent.v1"
    assert payload["instance_id"] == agent_server.agent.instance_id
    assert payload["pid"] == os.getpid()
    body = response.text
    for forbidden in (
        str(agent_server.root),
        str(agent_server.runtime_dir),
        str(agent_server.socket_path),
        agent_server.token,
    ):
        assert forbidden not in body


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-token", "Basic dXNlcjpwYXNz", "Bearer", "Token abc12345"],
)
def test_v1_requires_a_valid_bearer_token(uds_client, authorization: str | None) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = uds_client.get("/v1/workspaces", headers=headers)
    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "unauthorized"
    assert set(body) == {"error", "message", "retryable", "details"}


def test_unauthenticated_reserved_route_does_not_reveal_itself(uds_client) -> None:
    response = uds_client.get("/v1/sessions")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_bearer_scheme_is_case_insensitive(uds_client, agent_server: RunningAgent) -> None:
    response = uds_client.get(
        "/v1/workspaces",
        headers={"Authorization": f"bearer {agent_server.token}"},
    )
    assert response.status_code == 200
    assert response.json()["workspaces"] == []


def test_sessions_routes_are_reserved_and_typed(uds_client, agent_server: RunningAgent) -> None:
    response = uds_client.get("/v1/sessions/anything", headers=agent_server.auth_headers)
    assert response.status_code == 404
    assert response.json()["error"] == "not_implemented"


@pytest.mark.parametrize(
    "path",
    [
        f"/v1/workspaces/{VALID_WORKSPACE_ID}/files",
        f"/v1/workspaces/{VALID_WORKSPACE_ID}/events",
    ],
)
def test_workspace_routes_are_not_implemented_in_the_skeleton(
    uds_client, agent_server: RunningAgent, path: str
) -> None:
    response = uds_client.get(path, headers=agent_server.auth_headers)
    assert response.status_code == 501
    assert response.json()["error"] == "not_implemented"


def test_unknown_v1_route_is_a_typed_404(uds_client, agent_server: RunningAgent) -> None:
    response = uds_client.get("/v1/does-not-exist", headers=agent_server.auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "invalid_request"
    assert set(body) == {"error", "message", "retryable", "details"}


def test_unknown_top_level_route_is_a_typed_404(uds_client) -> None:
    response = uds_client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"] == "invalid_request"


def test_chunked_body_is_refused_with_411(agent_server: RunningAgent) -> None:
    request = (
        b"POST /v1/workspaces/ensure HTTP/1.1\r\n"
        b"Host: workspace-agent\r\n"
        + f"Authorization: Bearer {agent_server.token}\r\n".encode("ascii")
        + b"Idempotency-Key: abcd1234\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"0\r\n\r\n"
    )
    raw = raw_http_request(agent_server.socket_path, request)
    assert response_status(raw) == 411
    assert response_json(raw)["error"] == "length_required"


def test_missing_content_length_is_refused_with_411(agent_server: RunningAgent) -> None:
    request = (
        b"POST /v1/workspaces/ensure HTTP/1.1\r\n"
        b"Host: workspace-agent\r\n"
        + f"Authorization: Bearer {agent_server.token}\r\n".encode("ascii")
        + b"Idempotency-Key: abcd1234\r\n"
        b"\r\n"
    )
    raw = raw_http_request(agent_server.socket_path, request)
    assert response_status(raw) == 411
    assert response_json(raw)["error"] == "length_required"


def test_post_requires_a_valid_idempotency_key(uds_client, agent_server: RunningAgent) -> None:
    response = uds_client.post(
        "/v1/workspaces/ensure", headers=agent_server.auth_headers, content=b"{}"
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_post_with_idempotency_key_reaches_the_ensure_handler(
    uds_client, agent_server: RunningAgent
) -> None:
    headers = {**agent_server.auth_headers, "Idempotency-Key": "m24-w1-key-0001"}
    response = uds_client.post("/v1/workspaces/ensure", headers=headers, content=b"{}")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_oversized_json_body_is_refused_with_413(agent_server: RunningAgent) -> None:
    declared = protocol.JSON_BODY_LIMIT + 1
    request = (
        b"POST /v1/workspaces/ensure HTTP/1.1\r\n"
        b"Host: workspace-agent\r\n"
        + f"Authorization: Bearer {agent_server.token}\r\n".encode("ascii")
        + b"Idempotency-Key: abcd1234\r\n"
        + f"Content-Length: {declared}\r\n".encode("ascii")
        + b"\r\n"
    )
    raw = raw_http_request(agent_server.socket_path, request, timeout=5.0)
    assert response_status(raw) == 413
    assert response_json(raw)["error"] == "size_limit"


def test_runtime_permission_modes(agent_server: RunningAgent) -> None:
    runtime = os.lstat(agent_server.runtime_dir)
    token = os.lstat(agent_server.runtime_dir / TOKEN_FILE_NAME)
    socket_info = os.lstat(agent_server.socket_path)
    assert stat.S_ISDIR(runtime.st_mode)
    assert stat.S_IMODE(runtime.st_mode) == 0o750
    assert stat.S_ISREG(token.st_mode)
    assert stat.S_IMODE(token.st_mode) == 0o640
    assert stat.S_ISSOCK(socket_info.st_mode)
    assert stat.S_IMODE(socket_info.st_mode) == 0o660


def test_bearer_token_is_confined_to_the_runtime_directory(
    agent_server: RunningAgent,
) -> None:
    token_bytes = agent_server.token.encode("ascii")
    token_path = agent_server.runtime_dir / TOKEN_FILE_NAME
    assert read_token_file(token_path) == agent_server.token
    assert token_path.parent == agent_server.runtime_dir
    for path in agent_server.root.rglob("*"):
        if path.is_file():
            assert token_bytes not in path.read_bytes()


def test_token_file_read_is_hardened_against_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(TokenFileError):
        read_token_file(link)


def test_generated_tokens_are_opaque_and_unique() -> None:
    tokens = {generate_token() for _ in range(8)}
    assert len(tokens) == 8
    assert all(len(token) >= 32 for token in tokens)


def test_group_or_other_writable_runtime_dir_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    os.chmod(runtime_dir, 0o770)
    agent = WorkspaceAgent(AgentConfig(root=root, socket_dir=runtime_dir))
    with pytest.raises(RuntimeDirectoryError):
        agent.start()


def test_foreign_runtime_dir_needs_the_explicit_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    os.chmod(runtime_dir, 0o750)
    foreign_uid = os.geteuid() + 1
    monkeypatch.setattr(os, "geteuid", lambda: foreign_uid)
    with pytest.raises(RuntimeDirectoryError):
        WorkspaceAgent(AgentConfig(root=root, socket_dir=runtime_dir)).start()
    agent = WorkspaceAgent(
        AgentConfig(root=root, socket_dir=runtime_dir, allow_foreign_runtime_dir=True)
    )
    agent.start()
    try:
        assert os.path.exists(runtime_dir / SOCKET_FILE_NAME)
    finally:
        agent.stop()


def test_existing_non_socket_path_is_never_deleted(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    os.chmod(runtime_dir, 0o750)
    placeholder = runtime_dir / SOCKET_FILE_NAME
    placeholder.write_text("not a socket")
    agent = WorkspaceAgent(AgentConfig(root=root, socket_dir=runtime_dir))
    with pytest.raises(SocketPathError):
        agent.start()
    assert placeholder.read_text() == "not a socket"


def test_stale_socket_path_is_replaced_and_removed_on_stop(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    os.chmod(runtime_dir, 0o750)
    socket_path = runtime_dir / SOCKET_FILE_NAME
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    assert stat.S_ISSOCK(os.lstat(socket_path).st_mode)
    agent = WorkspaceAgent(AgentConfig(root=root, socket_dir=runtime_dir))
    agent.start()
    try:
        assert os.path.exists(socket_path)
        assert stat.S_ISSOCK(os.lstat(socket_path).st_mode)
    finally:
        agent.stop()
    assert not os.path.exists(socket_path)


def test_second_instance_exits_73(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = tmp_path / "run"
    env = agent_environment()
    argv = agent_argv(root, runtime_dir)
    first = subprocess.Popen(
        argv,
        cwd=REPOSITORY_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_health(runtime_dir / SOCKET_FILE_NAME)
        live_token = read_token_file(runtime_dir / TOKEN_FILE_NAME)
        second = subprocess.run(
            argv,
            cwd=REPOSITORY_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert second.returncode == 73, second.stderr
        # A losing start must not rotate the token out from under the live agent.
        assert read_token_file(runtime_dir / TOKEN_FILE_NAME) == live_token
        first.terminate()
        assert first.wait(timeout=20) == 0
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=20)
    assert not os.path.exists(runtime_dir / SOCKET_FILE_NAME)

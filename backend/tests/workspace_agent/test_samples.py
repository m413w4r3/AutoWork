"""``base_samples`` materialization and read-only access (design §8, ticket W5).

All tests are offline and rootless: a real agent serves an AF_UNIX socket over a
pytest tmpdir, and the direct-API tests drive :mod:`sidecars.workspace_agent.samples`
against a real workspace tree.
"""

from __future__ import annotations

import hashlib
import io
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sidecars.workspace_agent import protocol, samples
from sidecars.workspace_agent.workspace import (
    EnsureWorkspaceRequest,
    WorkspaceDescriptor,
    WorkspaceManager,
)

from tests.workspace_agent.conftest import (
    RunningAgent,
    raw_http_request,
    response_json,
    response_status,
)

DELIVERY_CODE = "IR2609"
ARTICLE_SLUG = "bluemoon-cn"
SUBJECT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
INVESTIGATION_ID = "22222222-2222-4222-8222-222222222222"
SYNTHESIS_SHA256 = "a" * 64

SAMPLE_BYTES = b"MZ" + bytes(range(256)) * 8
SAMPLE_SHA256 = hashlib.sha256(SAMPLE_BYTES).hexdigest()
OTHER_SHA256 = hashlib.sha256(b"another sample").hexdigest()


def _ensure_payload() -> dict[str, object]:
    return {
        "delivery_code": DELIVERY_CODE,
        "article_slug": ARTICLE_SLUG,
        "subject_id": SUBJECT_ID,
        "investigation_id": INVESTIGATION_ID,
        "synthesis_sha256": SYNTHESIS_SHA256,
    }


def ensure_workspace(
    client: httpx.Client, agent_server: RunningAgent, *, key: str = "m24-w5-ensure-0001"
) -> str:
    response = client.post(
        "/v1/workspaces/ensure",
        headers={**agent_server.auth_headers, "Idempotency-Key": key},
        json=_ensure_payload(),
    )
    assert response.status_code == 201, response.text
    workspace_id: object = response.json()["workspace_id"]
    assert isinstance(workspace_id, str)
    return workspace_id


def workspace_directory(agent_server: RunningAgent) -> Path:
    return agent_server.root / DELIVERY_CODE / ARTICLE_SLUG


def sample_url(workspace_id: str, sha256: str) -> str:
    return f"/v1/workspaces/{workspace_id}/base-samples/{sha256}"


def post_sample(
    client: httpx.Client,
    agent_server: RunningAgent,
    workspace_id: str,
    sha256: str,
    content: bytes,
    *,
    key: str,
) -> httpx.Response:
    return client.post(
        sample_url(workspace_id, sha256),
        headers={**agent_server.auth_headers, "Idempotency-Key": key},
        content=content,
    )


def incoming_entries(workspace: Path) -> list[str]:
    return sorted(os.listdir(workspace / ".autowork" / "incoming"))


def send_half_closed(socket_path: Path, payload: bytes) -> bytes:
    """Send ``payload``, half-close the write side, then read the response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10.0)
        connection.connect(str(socket_path))
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def raw_post(
    agent_server: RunningAgent,
    target: str,
    *,
    content_length: int | None,
    extra_headers: bytes = b"",
    body: bytes = b"",
) -> bytes:
    length_line = (
        b"" if content_length is None else f"Content-Length: {content_length}\r\n".encode()
    )
    request = (
        f"POST {target} HTTP/1.1\r\nHost: workspace-agent\r\n".encode()
        + f"Authorization: Bearer {agent_server.token}\r\n".encode()
        + b"Idempotency-Key: m24-w5-raw-0001\r\n"
        + extra_headers
        + length_line
        + b"\r\n"
        + body
    )
    return raw_http_request(agent_server.socket_path, request)


def raw_get(agent_server: RunningAgent, target: str) -> bytes:
    request = (
        f"GET {target} HTTP/1.1\r\nHost: workspace-agent\r\n".encode()
        + f"Authorization: Bearer {agent_server.token}\r\n".encode()
        + b"\r\n"
    )
    return raw_http_request(agent_server.socket_path, request)


def test_materializes_a_0440_sample_in_a_0750_directory(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)

    response = post_sample(
        uds_client, agent_server, workspace_id, SAMPLE_SHA256, SAMPLE_BYTES, key="m24-w5-post-0001"
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "sha256": SAMPLE_SHA256,
        "path": f"base_samples/{SAMPLE_SHA256}",
        "size": len(SAMPLE_BYTES),
        "mode": "0440",
        "already_present": False,
    }
    workspace = workspace_directory(agent_server)
    materialized = workspace / "base_samples" / SAMPLE_SHA256
    assert materialized.read_bytes() == SAMPLE_BYTES
    info = os.lstat(materialized)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o440
    assert stat.S_IMODE(info.st_mode) & 0o111 == 0
    assert stat.S_IMODE(os.lstat(workspace / "base_samples").st_mode) == 0o750
    assert incoming_entries(workspace) == []


def test_hash_mismatch_leaves_neither_a_sample_nor_a_part_file(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    tampered = b"tampered payload"

    response = post_sample(
        uds_client, agent_server, workspace_id, SAMPLE_SHA256, tampered, key="m24-w5-post-0100"
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "hash_mismatch"
    assert body["details"]["reason"] == "content_mismatch"
    workspace = workspace_directory(agent_server)
    assert os.listdir(workspace / "base_samples") == []
    assert incoming_entries(workspace) == []
    assert str(agent_server.root) not in response.text


def test_rematerialization_verifies_the_existing_entry_and_never_rewrites_it(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    first = post_sample(
        uds_client, agent_server, workspace_id, SAMPLE_SHA256, SAMPLE_BYTES, key="m24-w5-post-0200"
    )
    assert first.status_code == 201
    workspace = workspace_directory(agent_server)
    materialized = workspace / "base_samples" / SAMPLE_SHA256
    before = os.lstat(materialized)

    second = post_sample(
        uds_client, agent_server, workspace_id, SAMPLE_SHA256, SAMPLE_BYTES, key="m24-w5-post-0201"
    )

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["already_present"] is True
    assert body["sha256"] == SAMPLE_SHA256
    assert body["path"] == f"base_samples/{SAMPLE_SHA256}"
    assert body["mode"] == "0440"
    after = os.lstat(materialized)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert materialized.read_bytes() == SAMPLE_BYTES
    assert incoming_entries(workspace) == []


def test_existing_wrong_content_under_the_digest_name_is_a_conflict_never_overwritten(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    workspace = workspace_directory(agent_server)
    planted = workspace / "base_samples" / SAMPLE_SHA256
    planted.write_bytes(b"planted bytes")
    os.chmod(planted, 0o440)
    before = os.lstat(planted)

    response = post_sample(
        uds_client, agent_server, workspace_id, SAMPLE_SHA256, SAMPLE_BYTES, key="m24-w5-post-0300"
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "hash_mismatch"
    assert body["details"]["reason"] == "existing_content_mismatch"
    assert body["details"]["existing_sha256"] == hashlib.sha256(b"planted bytes").hexdigest()
    assert planted.read_bytes() == b"planted bytes"
    after = os.lstat(planted)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert incoming_entries(workspace) == []


def test_symlink_planted_at_the_digest_name_is_refused(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    workspace = workspace_directory(agent_server)
    target = workspace / "notes" / "humaines" / "target.bin"
    target.write_bytes(SAMPLE_BYTES)
    link = workspace / "base_samples" / SAMPLE_SHA256
    link.symlink_to(target)

    response = post_sample(
        uds_client, agent_server, workspace_id, SAMPLE_SHA256, SAMPLE_BYTES, key="m24-w5-post-0400"
    )

    assert response.status_code == 409
    assert response.json()["error"] == "concurrent_change"
    assert os.path.islink(link)
    assert target.read_bytes() == SAMPLE_BYTES
    assert incoming_entries(workspace) == []

    read = uds_client.get(
        sample_url(workspace_id, SAMPLE_SHA256), headers=agent_server.auth_headers
    )
    assert read.status_code == 409
    assert read.json()["error"] == "concurrent_change"
    assert os.path.islink(link)


def test_chunked_upload_is_refused_with_411(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)

    raw = raw_post(
        agent_server,
        sample_url(workspace_id, SAMPLE_SHA256),
        content_length=None,
        extra_headers=b"Transfer-Encoding: chunked\r\n",
        body=f"{len(SAMPLE_BYTES):x}\r\n".encode() + SAMPLE_BYTES + b"\r\n0\r\n\r\n",
    )

    assert response_status(raw) == 411
    assert response_json(raw)["error"] == "length_required"
    workspace = workspace_directory(agent_server)
    assert os.listdir(workspace / "base_samples") == []
    assert incoming_entries(workspace) == []


def test_missing_content_length_is_refused_with_411(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)

    raw = raw_post(agent_server, sample_url(workspace_id, SAMPLE_SHA256), content_length=None)

    assert response_status(raw) == 411
    assert response_json(raw)["error"] == "length_required"


def test_declared_size_above_the_sample_limit_is_refused_with_413(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)

    raw = raw_post(
        agent_server,
        sample_url(workspace_id, SAMPLE_SHA256),
        content_length=protocol.SAMPLE_LIMIT + 1,
    )

    assert response_status(raw) == 413
    assert response_json(raw)["error"] == "size_limit"


def test_truncated_upload_leaves_neither_a_sample_nor_a_part_file(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    head = (
        f"POST {sample_url(workspace_id, SAMPLE_SHA256)} HTTP/1.1\r\n".encode()
        + b"Host: workspace-agent\r\n"
        + f"Authorization: Bearer {agent_server.token}\r\n".encode()
        + b"Idempotency-Key: m24-w5-raw-0002\r\n"
        + f"Content-Length: {len(SAMPLE_BYTES)}\r\n".encode()
        + b"\r\n"
    )

    raw = send_half_closed(agent_server.socket_path, head + SAMPLE_BYTES[: len(SAMPLE_BYTES) // 2])

    assert response_status(raw) == 400
    body = response_json(raw)
    assert body["error"] == "invalid_request"
    assert body["details"]["reason"] == "truncated_body"
    workspace = workspace_directory(agent_server)
    assert os.listdir(workspace / "base_samples") == []
    assert incoming_entries(workspace) == []


def test_path_traversal_and_malformed_digest_segments_are_rejected(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    workspace = workspace_directory(agent_server)
    targets = (
        f"/v1/workspaces/{workspace_id}/base-samples/../../../../etc/passwd",
        f"/v1/workspaces/{workspace_id}/base-samples/..%2f..%2fetc%2fpasswd",
        f"/v1/workspaces/{workspace_id}/base-samples/{SAMPLE_SHA256.upper()}",
        f"/v1/workspaces/{workspace_id}/base-samples/{'a' * 63}",
        f"/v1/workspaces/{workspace_id}/base-samples/{'a' * 65}",
    )
    for target in targets:
        get_raw = raw_get(agent_server, target)
        assert response_status(get_raw) == 404, target
        assert response_json(get_raw)["error"] == "invalid_request", target
        post_raw = raw_post(agent_server, target, content_length=1, body=b"x")
        assert response_status(post_raw) == 404, target
        assert response_json(post_raw)["error"] == "invalid_request", target
    assert os.listdir(workspace / "base_samples") == []
    assert incoming_entries(workspace) == []


def test_get_returns_the_raw_sample_bytes(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    created = post_sample(
        uds_client, agent_server, workspace_id, SAMPLE_SHA256, SAMPLE_BYTES, key="m24-w5-post-0500"
    )
    assert created.status_code == 201

    response = uds_client.get(
        sample_url(workspace_id, SAMPLE_SHA256), headers=agent_server.auth_headers
    )

    assert response.status_code == 200
    assert response.content == SAMPLE_BYTES
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-length"] == str(len(SAMPLE_BYTES))

    missing = uds_client.get(
        sample_url(workspace_id, OTHER_SHA256), headers=agent_server.auth_headers
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "invalid_request"
    assert missing.json()["details"]["sha256"] == OTHER_SHA256


def test_unknown_workspace_is_not_found(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    unknown = "ws-" + "0" * 32
    created = post_sample(
        uds_client, agent_server, unknown, SAMPLE_SHA256, SAMPLE_BYTES, key="m24-w5-post-0600"
    )
    assert created.status_code == 404
    assert created.json()["error"] == "workspace_not_found"
    read = uds_client.get(sample_url(unknown, SAMPLE_SHA256), headers=agent_server.auth_headers)
    assert read.status_code == 404
    assert read.json()["error"] == "workspace_not_found"


def test_a_missing_base_samples_directory_is_recreated_with_0750(
    uds_client: httpx.Client, agent_server: RunningAgent
) -> None:
    workspace_id = ensure_workspace(uds_client, agent_server)
    workspace = workspace_directory(agent_server)
    (workspace / "base_samples").rmdir()

    response = post_sample(
        uds_client, agent_server, workspace_id, SAMPLE_SHA256, SAMPLE_BYTES, key="m24-w5-post-0700"
    )

    assert response.status_code == 201, response.text
    assert stat.S_IMODE(os.lstat(workspace / "base_samples").st_mode) == 0o750
    assert (workspace / "base_samples" / SAMPLE_SHA256).read_bytes() == SAMPLE_BYTES


def _direct_workspace(tmp_path: Path) -> tuple[Path, WorkspaceDescriptor]:
    root = tmp_path / "root"
    descriptor = WorkspaceManager(root).ensure(
        EnsureWorkspaceRequest.from_mapping(_ensure_payload())
    )
    return root, descriptor


class _ProbeStream:
    """A byte source that runs a probe on the first read."""

    def __init__(self, payload: bytes, probe: Callable[[], None]) -> None:
        self._payload = payload
        self._probe = probe
        self._probed = False

    def read(self, size: int) -> bytes:
        if not self._probed:
            self._probed = True
            self._probe()
        chunk = self._payload[:size]
        self._payload = self._payload[size:]
        return chunk


def test_the_part_file_is_0600_and_nothing_is_published_before_verification(
    tmp_path: Path,
) -> None:
    root, descriptor = _direct_workspace(tmp_path)
    workspace = root / DELIVERY_CODE / ARTICLE_SLUG
    observed: list[list[tuple[str, int]]] = []

    def probe() -> None:
        assert os.listdir(workspace / "base_samples") == []
        incoming = workspace / ".autowork" / "incoming"
        observed.append(
            [
                (name, stat.S_IMODE(os.lstat(incoming / name).st_mode))
                for name in os.listdir(incoming)
            ]
        )

    result = samples.materialize_sample(
        root=root,
        descriptor=descriptor,
        sha256=SAMPLE_SHA256,
        source=_ProbeStream(SAMPLE_BYTES, probe),
        declared_length=len(SAMPLE_BYTES),
    )

    assert result.already_present is False
    assert len(observed) == 1
    assert len(observed[0]) == 1
    part_name, part_mode = observed[0][0]
    assert part_name.endswith(".part")
    assert part_mode == 0o600
    materialized = workspace / "base_samples" / SAMPLE_SHA256
    assert stat.S_IMODE(os.lstat(materialized).st_mode) == 0o440
    assert sorted(os.listdir(workspace / ".autowork" / "incoming")) == []


def test_post_publication_hash_mismatch_unlinks_the_published_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, descriptor = _direct_workspace(tmp_path)
    workspace = root / DELIVERY_CODE / ARTICLE_SLUG

    def wrong_digest(fd: int) -> str:
        del fd
        return "0" * 64

    monkeypatch.setattr(samples, "_hash_fd", wrong_digest)

    with pytest.raises(samples.SampleHashMismatchError) as excinfo:
        samples.materialize_sample(
            root=root,
            descriptor=descriptor,
            sha256=SAMPLE_SHA256,
            source=io.BytesIO(SAMPLE_BYTES),
            declared_length=len(SAMPLE_BYTES),
        )

    assert excinfo.value.code is protocol.ErrorCode.HASH_MISMATCH
    assert excinfo.value.status == 422
    assert excinfo.value.details["reason"] == "post_publication_mismatch"
    assert os.listdir(workspace / "base_samples") == []
    assert sorted(os.listdir(workspace / ".autowork" / "incoming")) == []


def test_analysis_gid_is_applied_when_configured_and_ignored_otherwise(
    tmp_path: Path,
) -> None:
    root, descriptor = _direct_workspace(tmp_path)
    workspace = root / DELIVERY_CODE / ARTICLE_SLUG

    unconfigured = samples.materialize_sample(
        root=root,
        descriptor=descriptor,
        sha256=hashlib.sha256(b"unconfigured").hexdigest(),
        source=io.BytesIO(b"unconfigured"),
        declared_length=len(b"unconfigured"),
    )
    assert (
        os.lstat(workspace / "base_samples" / unconfigured.sha256).st_gid
        == os.stat(workspace / "base_samples").st_gid
    )

    configured = samples.materialize_sample(
        root=root,
        descriptor=descriptor,
        sha256=hashlib.sha256(b"configured").hexdigest(),
        source=io.BytesIO(b"configured"),
        declared_length=len(b"configured"),
        analysis_gid=os.getgid(),
    )
    assert os.lstat(workspace / "base_samples" / configured.sha256).st_gid == os.getgid()

    forbidden_gid = os.getgid() + 500_000
    tolerated = samples.materialize_sample(
        root=root,
        descriptor=descriptor,
        sha256=hashlib.sha256(b"tolerated").hexdigest(),
        source=io.BytesIO(b"tolerated"),
        declared_length=len(b"tolerated"),
        analysis_gid=forbidden_gid,
    )
    info = os.lstat(workspace / "base_samples" / tolerated.sha256)
    if os.geteuid() != 0:
        assert info.st_gid == os.getgid()
    assert stat.S_IMODE(info.st_mode) == 0o440

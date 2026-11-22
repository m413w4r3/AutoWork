"""Backend client wire contract against the workspace agent (ticket W6).

Half of these tests drive the real client through ``httpx.MockTransport`` to
pin the documented §4 shapes, bounds, idempotency and the single 401 token
reload. The last test runs the REAL stdlib agent on a tmpdir AF_UNIX socket and
drives it with the real client: ensure, get, list and base-sample
materialization (bytes and streamed), with no network and no root.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from cti_app.application.workspace_agent import (
    CONTENT_READ_LIMIT,
    JSON_RESPONSE_LIMIT,
    EnsureWorkspaceV1,
    WireErrorCode,
    WorkspaceAgentErrorEnvelopeV1,
    WorkspaceProtocolError,
    WorkspaceResponseTooLargeError,
    WorkspaceUnauthorizedError,
)
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient
from tests.workspace_agent.conftest import RunningAgent

WORKSPACE_ID = "ws-" + "1" * 32
SUBJECT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
INVESTIGATION_ID = "22222222-2222-4222-8222-222222222222"
SHA_A = "a" * 64
SHA_B = "b" * 64
REVISION_ID = "rev-" + "2" * 32


def _manifest() -> dict[str, object]:
    return {
        "schema": "autowork-workspace-manifest.v1",
        "workspace_id": WORKSPACE_ID,
        "delivery_code": "IR2609",
        "article_slug": "bluemoon-cn",
        "subject_id": SUBJECT_ID,
        "investigation_id": INVESTIGATION_ID,
        "synthesis_sha256": SHA_A,
        "created_at": "2026-11-22T12:00:00Z",
        "created_by": "autowork-workspace-agent",
        "layout_version": 1,
        "identity_sha256": SHA_B,
    }


def _workspace(created: bool = True) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "host_path": "/synthetic/root/IR2609/bluemoon-cn",
        "created": created,
        "manifest": _manifest(),
    }


def _revision(path: str = "notes/humaines/note.txt") -> dict[str, object]:
    return {
        "revision_id": REVISION_ID,
        "path": path,
        "sha256": SHA_A,
        "size": 4,
        "observed_at": "2026-11-22T12:00:00Z",
        "origin": "ui",
        "task_id": None,
        "supersedes_revision_id": None,
    }


def _file_state(path: str = "notes/humaines/note.txt") -> dict[str, object]:
    return {
        "path": path,
        "revision_id": REVISION_ID,
        "sha256": SHA_A,
        "size": 4,
        "mode": "0640",
        "mtime_ns": 1_700_000_000_000_000_000,
        "origin": "ui",
        "observed_at": "2026-11-22T12:00:00Z",
        "policy_class": "human_editable",
        "task_id": None,
    }


def _ensure_request() -> EnsureWorkspaceV1:
    return EnsureWorkspaceV1(
        delivery_code="IR2609",
        article_slug="bluemoon-cn",
        subject_id=SUBJECT_ID,
        investigation_id=INVESTIGATION_ID,
        synthesis_sha256=SHA_A,
    )


def _error(status: int, code: WireErrorCode, message: str = "synthetic failure") -> httpx.Response:
    envelope = WorkspaceAgentErrorEnvelopeV1(
        error=code,
        message=message,
        retryable=False,
        details={},
    )
    return httpx.Response(status, json=envelope.model_dump(mode="json"))


def _write_token(path: Path, token: str) -> Path:
    path.write_text(token, encoding="ascii")
    return path


def _client(
    tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]
) -> HttpWorkspaceAgentClient:
    token_file = _write_token(tmp_path / "auth-token", "synthetic-token")
    return HttpWorkspaceAgentClient(
        socket_path=str(tmp_path / "workspace-agent.sock"),
        token_file=token_file,
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )


async def test_401_reloads_token_and_retries_exactly_once(tmp_path: Path) -> None:
    token_file = _write_token(tmp_path / "auth-token", "rotating-token")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            _write_token(token_file, "rotated-token")
            return _error(401, "unauthorized")
        return httpx.Response(200, json=_workspace(created=False))

    client = HttpWorkspaceAgentClient(
        socket_path=str(tmp_path / "workspace-agent.sock"),
        token_file=token_file,
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )
    try:
        workspace = await client.ensure_workspace(_ensure_request())
    finally:
        await client.aclose()
    assert workspace.workspace_id == WORKSPACE_ID
    assert len(seen) == 2
    assert seen[0].headers["authorization"] == "Bearer rotating-token"
    assert seen[1].headers["authorization"] == "Bearer rotated-token"


async def test_two_401s_raise_authorized_error_after_one_retry(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _error(401, "unauthorized", "bad token")

    client = _client(tmp_path, handler)
    try:
        with pytest.raises(WorkspaceUnauthorizedError):
            await client.list_workspaces()
    finally:
        await client.aclose()
    assert len(seen) == 2


async def test_streamed_sample_body_is_never_retried_on_401(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    payload = b"MZ" + bytes(range(64))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _error(401, "unauthorized")

    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(payload), 7):
            yield payload[offset : offset + 7]

    client = _client(tmp_path, handler)
    try:
        with pytest.raises(WorkspaceUnauthorizedError):
            await client.materialize_base_sample(
                WORKSPACE_ID,
                sha256=hashlib.sha256(payload).hexdigest(),
                content=chunks(),
                size=len(payload),
            )
    finally:
        await client.aclose()
    assert len(seen) == 1


async def test_unknown_wire_field_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workspaces": [], "unexpected": True})

    client = _client(tmp_path, handler)
    try:
        with pytest.raises(WorkspaceProtocolError):
            await client.list_workspaces()
    finally:
        await client.aclose()


async def test_oversized_json_response_is_rejected_before_reading(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(JSON_RESPONSE_LIMIT + 1)},
            content=b"",
        )

    client = _client(tmp_path, handler)
    try:
        with pytest.raises(WorkspaceResponseTooLargeError):
            await client.list_workspaces()
    finally:
        await client.aclose()


async def test_oversized_content_read_is_rejected_before_reading(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(CONTENT_READ_LIMIT + 1),
                "X-Autowork-Sha256": SHA_A,
                "X-Autowork-Mtime-Ns": "1",
                "X-Autowork-Size": str(CONTENT_READ_LIMIT + 1),
            },
            content=b"",
        )

    client = _client(tmp_path, handler)
    try:
        with pytest.raises(WorkspaceResponseTooLargeError):
            await client.read_content(WORKSPACE_ID, path="scripts/tool.py")
    finally:
        await client.aclose()


async def test_wire_operations_send_the_documented_shape(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    body = b"note"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/workspaces/ensure":
            return httpx.Response(201, json=_workspace())
        if path.endswith("/scan"):
            return httpx.Response(
                200,
                json={
                    "generation": 3,
                    "latest_cursor": 9,
                    "emitted": 2,
                    "policy_violations": 0,
                },
            )
        if request.method == "PUT" and path.endswith("/files"):
            return httpx.Response(
                200,
                json={
                    "revision": {**_revision(), "sha256": hashlib.sha256(body).hexdigest()},
                    "created": True,
                    "already_present": False,
                    "conflict": False,
                },
            )
        if request.method == "PUT" and path.endswith("/generated"):
            return httpx.Response(
                200,
                json={
                    "revision": {**_revision(), "sha256": hashlib.sha256(body).hexdigest()},
                    "created": True,
                    "already_present": False,
                    "conflict": False,
                },
            )
        if path.endswith("/files/revisions"):
            return httpx.Response(200, json={"revisions": [_revision()]})
        if path.endswith("/files/content"):
            return httpx.Response(
                200,
                headers={
                    "Content-Length": str(len(body)),
                    "ETag": f'"sha256:{hashlib.sha256(body).hexdigest()}"',
                    "X-Autowork-Sha256": hashlib.sha256(body).hexdigest(),
                    "X-Autowork-Mtime-Ns": "1700000000000000000",
                    "X-Autowork-Size": str(len(body)),
                },
                content=body,
            )
        if path.endswith("/snapshot") or path.endswith("/files"):
            return httpx.Response(
                200,
                json={"generation": 3, "files": [_file_state()], "next_page_token": None},
            )
        if path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "generation": 3,
                    "events": [],
                    "next_cursor": 9,
                    "oldest_retained": 1,
                    "resync_required": False,
                },
            )
        if "/base-samples/" in path:
            sha256 = path.rsplit("/", 1)[-1]
            return httpx.Response(
                201,
                json={
                    "sha256": sha256,
                    "path": f"base_samples/{sha256}",
                    "size": len(body),
                    "mode": "0440",
                    "already_present": False,
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = _client(tmp_path, handler)
    try:
        await client.ensure_workspace(_ensure_request())
        await client.scan_workspace(WORKSPACE_ID, full=True)
        await client.snapshot(WORKSPACE_ID, page_token="p1", limit=25)
        await client.events(WORKSPACE_ID, after=7, limit=42)
        await client.list_files(WORKSPACE_ID, prefix="notes/", page_token="p2", limit=10)
        await client.list_revisions(WORKSPACE_ID, path="notes/humaines/note.txt")
        content = await client.read_content(WORKSPACE_ID, path="notes/humaines/note.txt")
        await client.put_file(
            WORKSPACE_ID,
            path="notes/humaines/note.txt",
            content=body,
            expected_sha256=SHA_A,
            origin="ui",
            task_id=INVESTIGATION_ID,
        )
        await client.put_generated(
            WORKSPACE_ID,
            path="plans/plan.json",
            content=body,
            expected_sha256="new",
            generated_by="autowork-backend",
            source_sha256=SHA_B,
        )
        await client.materialize_base_sample(
            WORKSPACE_ID,
            sha256=hashlib.sha256(body).hexdigest(),
            content=body,
            size=len(body),
        )
    finally:
        await client.aclose()

    assert content.content == body
    assert content.size == len(body)
    by_path: dict[str, httpx.Request] = {}
    for request in seen:
        by_path[f"{request.method} {request.url.path}"] = request
    assert "idempotency-key" not in by_path[f"GET /v1/workspaces/{WORKSPACE_ID}/events"].headers
    ensure = by_path["POST /v1/workspaces/ensure"]
    assert "idempotency-key" in ensure.headers
    assert json.loads(ensure.content)["delivery_code"] == "IR2609"
    scan = by_path[f"POST /v1/workspaces/{WORKSPACE_ID}/scan"]
    assert json.loads(scan.content) == {"full": True}
    events = by_path[f"GET /v1/workspaces/{WORKSPACE_ID}/events"]
    assert events.url.params["after"] == "7"
    assert events.url.params["limit"] == "42"
    snapshot = by_path[f"GET /v1/workspaces/{WORKSPACE_ID}/snapshot"]
    assert snapshot.url.params["page_token"] == "p1"
    put_file = by_path[f"PUT /v1/workspaces/{WORKSPACE_ID}/files"]
    assert put_file.headers["x-autowork-expected-sha256"] == SHA_A
    assert put_file.headers["x-autowork-origin"] == "ui"
    assert put_file.headers["x-autowork-task-id"] == INVESTIGATION_ID
    assert "idempotency-key" in put_file.headers
    put_generated = by_path[f"PUT /v1/workspaces/{WORKSPACE_ID}/generated"]
    assert put_generated.headers["x-autowork-origin"] == "system"
    assert put_generated.headers["x-autowork-generated-by"] == "autowork-backend"
    assert put_generated.headers["x-autowork-source-sha256"] == SHA_B
    sample = by_path[
        f"POST /v1/workspaces/{WORKSPACE_ID}/base-samples/{hashlib.sha256(body).hexdigest()}"
    ]
    assert sample.headers["content-length"] == str(len(body))
    assert "idempotency-key" in sample.headers


async def test_real_sidecar_contract_ensure_get_list_materialize_sample(
    agent_server: RunningAgent,
) -> None:
    client = HttpWorkspaceAgentClient(
        socket_path=str(agent_server.socket_path),
        token_file=agent_server.runtime_dir / "auth-token",
        timeout_seconds=10.0,
    )
    sample = b"MZ" + bytes(range(256))
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    streamed = b"\x7fELF" + bytes(range(128))
    streamed_sha256 = hashlib.sha256(streamed).hexdigest()

    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(streamed), 9):
            yield streamed[offset : offset + 9]

    try:
        probe = await client.probe()
        assert probe.status == "available"
        assert probe.instance_id == agent_server.agent.instance_id

        created = await client.ensure_workspace(_ensure_request())
        expected_id = "ws-" + hashlib.sha256(b"IR2609\x00bluemoon-cn").hexdigest()[:32]
        assert created.workspace_id == expected_id
        assert created.created is True
        assert created.manifest.investigation_id.hex == INVESTIGATION_ID.replace("-", "")

        fetched = await client.get_workspace(created.workspace_id)
        assert fetched.workspace_id == created.workspace_id
        assert fetched.created is False

        listed = await client.list_workspaces()
        assert [item.workspace_id for item in listed.workspaces] == [expected_id]
        assert listed.workspaces[0].delivery_code == "IR2609"

        first = await client.materialize_base_sample(
            created.workspace_id,
            sha256=sample_sha256,
            content=sample,
            size=len(sample),
        )
        assert first.sha256 == sample_sha256
        assert first.path == f"base_samples/{sample_sha256}"
        assert first.mode == "0440"
        assert first.already_present is False

        replayed = await client.materialize_base_sample(
            created.workspace_id,
            sha256=sample_sha256,
            content=sample,
            size=len(sample),
        )
        assert replayed.already_present is True

        streamed_result = await client.materialize_base_sample(
            created.workspace_id,
            sha256=streamed_sha256,
            content=chunks(),
            size=len(streamed),
        )
        assert streamed_result.sha256 == streamed_sha256
        assert streamed_result.already_present is False
    finally:
        await client.aclose()


def test_error_envelope_helper_is_closed() -> None:
    with pytest.raises(ValidationError):
        WorkspaceAgentErrorEnvelopeV1.model_validate(
            {"error": "not_a_code", "message": "x", "retryable": False, "details": {}}
        )

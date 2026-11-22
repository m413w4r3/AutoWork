"""M24 lot B: host-session wire client, adapters and one-time secret delivery.

The last test drives the real sidecar ``ThreadingHTTPServer`` + H1 supervisor
with the fake profile over a tmpdir AF_UNIX socket, through the real adapters
and the real httpx client.  No real CLI, provider or network is involved: this
proves the backend/adapter wire contract, never CLI semantics.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from cti_app.application.agentic_host_sessions import (
    HOST_RESPONSE_LIMIT,
    HostCapabilityUnavailableError,
    HostCapturedFileV1,
    HostCollectRequestV1,
    HostHandoffRefV1,
    HostInstructionRequestV1,
    HostLogLineV1,
    HostLogRangeV1,
    HostLogsPageV1,
    HostPrepareSessionRequestV1,
    HostReasonRequestV1,
    HostReconcileRequestV1,
    HostResumeRequestV1,
    HostSecretHandoffConsumedError,
    HostSecretUnavailableError,
    HostSessionResultV1,
    HostSessionStateConflictError,
    HostStartSessionRequestV1,
    HostToolManifestEntryV1,
    HostToolManifestRequestV1,
)
from cti_app.application.agentic_sessions import SessionSecret
from cti_app.application.workspace_agent import (
    IDEMPOTENCY_KEY_PATTERN,
    WorkspaceLocalUnavailableError,
    WorkspaceProtocolError,
    WorkspaceUnauthorizedError,
    WorkspaceUnconfiguredError,
)
from cti_app.config import Settings
from cti_app.domain.agentic_operator import (
    CollectResultsRequest,
    InstructionDeliveryOutcome,
    InterruptSessionRequest,
    OperatorAdapterKind,
    PrepareSessionRequest,
    ProbeCapabilitiesRequest,
    ReconcileSessionRequest,
    SendInstructionRequest,
    SessionReconcileOutcome,
    SessionStatusRequest,
    StartSessionRequest,
)
from cti_app.infrastructure.agentic.host_operator import (
    HostOperatorAdapter,
    HostSessionPreparationRegistry,
    host_idempotency_key,
)
from cti_app.infrastructure.agentic.host_sessions import HttpHostSessionsClient
from cti_app.infrastructure.agentic.secret_delivery import HostSessionSecretDelivery
from cti_app.infrastructure.agentic.wiring import (
    AgenticOperatorPorts,
    build_agentic_operator_ports,
)
from tests.agentic_support import (
    HANDOFF_ID,
    INVESTIGATION_ID,
    NOW,
    SECRET_BYTES,
    SECRET_HEX,
    SESSION_ID,
    TASK_ID,
    WORKSPACE_ID,
    FakeHostSessionsPort,
    FixedContextProvider,
    context,
    probe_wire,
    status_wire,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sidecars.workspace_agent.auth import TOKEN_FILE_NAME  # noqa: E402
from sidecars.workspace_agent.sessions import supervisor as supervisor_module  # noqa: E402
from sidecars.workspace_agent.sessions.supervisor import (  # noqa: E402
    FakeProfile,
    ProfileRegistry,
)

from tests.workspace_agent.sessions_support import (  # noqa: E402
    RunningSessionAgent,
    fake_script,
    running_session_agent,
    write_fake_script,
)

_IDEMPOTENCY_KEY = re.compile(IDEMPOTENCY_KEY_PATTERN)
_PREPARE_KEY = "m24h0-prepare-0001"
_SECRET_B64 = base64.b64encode(SECRET_BYTES).decode("ascii")


class _Wire:
    """A recording ``httpx`` transport for the typed client."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[tuple[str, str], httpx.Response | tuple[int, object]] = {}

    def route(
        self,
        method: str,
        path: str,
        body: object | httpx.Response,
        *,
        status: int = 200,
    ) -> None:
        if isinstance(body, httpx.Response):
            self.responses[(method, path)] = body
        else:
            self.responses[(method, path)] = (status, body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses[(request.method, request.url.path)]
        if isinstance(response, httpx.Response):
            return response
        status, body = response
        return httpx.Response(status, json=body)

    def paths(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]


def _token_file(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path: Path = base / str(TOKEN_FILE_NAME)
    path.write_text("m24-lotb-bearer-token-0123456789abcdef", encoding="ascii")
    return path


def _client(
    base: Path, handler: Callable[[httpx.Request], httpx.Response]
) -> HttpHostSessionsClient:
    return HttpHostSessionsClient(
        socket_path=str(base / "agent.sock"),
        token_file=str(_token_file(base)),
        transport=httpx.MockTransport(handler),
    )


def _prepare_request(capability_sha256: str) -> PrepareSessionRequest:
    return PrepareSessionRequest(
        session_id=SESSION_ID,
        investigation_id=INVESTIGATION_ID,
        task_id=TASK_ID,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        handoff_id=HANDOFF_ID,
        capability_sha256=capability_sha256,
    )


def _prepare_wire() -> HostPrepareSessionRequestV1:
    return HostPrepareSessionRequestV1(
        session_id=SESSION_ID,
        adapter_kind="codex_cli",
        workspace_id=WORKSPACE_ID,
        investigation_id=INVESTIGATION_ID,
        task_id=TASK_ID,
        handoff=HostHandoffRefV1(handoff_id=HANDOFF_ID, version=1, sha256="b" * 64),
        lease_fence=0,
        allowed_workspace_paths=("notes/humaines", "results"),
        skill_names=(),
        budgets=context().budgets,
        model="fake-model",
        auth_secret=_SECRET_B64,
    )


def _codex_adapter(
    host: FakeHostSessionsPort, preparations: HostSessionPreparationRegistry
) -> HostOperatorAdapter:
    # The base class is the adapter; this keeps the shared assertions typed.
    from cti_app.infrastructure.agentic.codex_cli import CodexCliOperatorAdapter

    return CodexCliOperatorAdapter(
        host=host,
        preparations=preparations,
        context=FixedContextProvider(),
        clock=lambda: NOW,
    )


def _standard_wire() -> _Wire:
    wire = _Wire()
    wire.route("GET", "/v1/sessions/capabilities", probe_wire().model_dump(mode="json"))
    wire.route(
        "POST",
        "/v1/sessions",
        {
            "session_id": str(SESSION_ID),
            "state": "ready",
            "state_version": 1,
            "capability_sha256": probe_wire().probe_sha256,
            "confinement": {"profile": "fake-direct", "bwrap_ok": False, "netns": False},
            "prompt_sha256": "9" * 64,
        },
    )
    wire.route(
        "POST",
        f"/v1/sessions/{SESSION_ID}/start",
        {
            "session_id": str(SESSION_ID),
            "state": "running",
            "state_version": 2,
            "run": 1,
            "log_cursor": 3,
        },
    )
    wire.route("GET", f"/v1/sessions/{SESSION_ID}", status_wire().model_dump(mode="json"))
    wire.route(
        "GET",
        f"/v1/sessions/{SESSION_ID}/events",
        {
            "generation": 1,
            "events": [
                {
                    "seq": 1,
                    "kind": "process_started",
                    "at": "2026-11-22T12:00:00Z",
                    "payload": {"run": 1},
                }
            ],
            "next_cursor": 1,
            "oldest_retained": 0,
            "resync_required": False,
        },
    )
    wire.route(
        "GET",
        f"/v1/sessions/{SESSION_ID}/logs",
        {
            "lines": [{"seq": 1, "stream": "stdout", "text": "started", "truncated": False}],
            "next_cursor": 1,
        },
    )
    wire.route(
        "POST",
        f"/v1/sessions/{SESSION_ID}/instructions",
        {"accepted": True, "delivered": "live", "cursor": 4},
    )
    wire.route(
        "POST",
        f"/v1/sessions/{SESSION_ID}/interrupt",
        {"state": "interrupt_requested", "signal_sent": True},
    )
    wire.route(
        "POST",
        f"/v1/sessions/{SESSION_ID}/resume",
        {
            "session_id": str(SESSION_ID),
            "state": "running",
            "state_version": 3,
            "run": 2,
            "log_cursor": 9,
        },
    )
    wire.route(
        "POST",
        f"/v1/sessions/{SESSION_ID}/reconcile",
        {
            "outcome": "terminated_clean",
            "evidence": {
                "last_event_seq": 7,
                "exit": {"code": 0, "signal": None},
                "process_start_ticks": 10,
            },
        },
    )
    wire.route(
        "POST",
        f"/v1/sessions/{SESSION_ID}/collect",
        {
            "outcome": "success",
            "exit": {"code": 0, "signal": None},
            "final_message_sha256": "8" * 64,
            "files": [{"path": "notes/agents/t/finding.md", "sha256": "a" * 64}],
            "log_range": {"cursor_from": 0, "cursor_to": 7},
            "captured_notes": ["notes/agents/t/finding.md"],
            "collected_at": "2026-11-22T12:00:00Z",
        },
    )
    wire.route(
        "POST",
        f"/v1/sessions/{SESSION_ID}/tool-manifest",
        {"accepted": True, "manifest_sha256": "c" * 64},
    )
    wire.route("POST", f"/v1/sessions/{SESSION_ID}/close", {"state": "closed"})
    return wire


async def test_port_maps_one_to_one_to_session_wire(tmp_path: Path) -> None:
    wire = _standard_wire()
    client = _client(tmp_path, wire)
    try:
        capabilities = await client.probe_capabilities("codex_cli")
        assert capabilities.adapter_kind == "codex_cli"
        prepared = await client.prepare_session(_prepare_wire(), idempotency_key=_PREPARE_KEY)
        assert prepared.state == "ready"
        started = await client.start_session(
            SESSION_ID,
            HostStartSessionRequestV1(),
            idempotency_key=host_idempotency_key("start", SESSION_ID),
        )
        assert started.run == 1
        status = await client.get_session_status(SESSION_ID)
        assert status.state == "running"
        events = await client.get_events(SESSION_ID, after=0, limit=10)
        assert events.events[0].kind == "process_started"
        logs = await client.get_logs(SESSION_ID, after=0, limit=10)
        assert logs.lines[0].text == "started"
        instruction = await client.send_instruction(
            SESSION_ID,
            HostInstructionRequestV1(
                instruction_id=UUID("11111111-2222-4333-8444-555555555555"),
                text="focus on the loader",
                sha256=hashlib.sha256(b"focus on the loader").hexdigest(),
            ),
            idempotency_key=host_idempotency_key("instruction", SESSION_ID, "route-key"),
        )
        assert instruction.accepted is True
        interrupted = await client.interrupt_session(
            SESSION_ID,
            HostReasonRequestV1(reason="bounded test"),
            idempotency_key=host_idempotency_key("interrupt", SESSION_ID, "bounded test"),
        )
        assert interrupted.signal_sent is True
        resumed = await client.resume_session(
            SESSION_ID,
            HostResumeRequestV1(reason="human resume"),
            idempotency_key=host_idempotency_key("resume", SESSION_ID),
        )
        assert resumed.run == 2
        reconciled = await client.reconcile_session(
            SESSION_ID,
            HostReconcileRequestV1(),
            idempotency_key=host_idempotency_key("reconcile", SESSION_ID),
        )
        assert reconciled.outcome == "terminated_clean"
        collected = await client.collect_results(
            SESSION_ID,
            HostCollectRequestV1(capture=True),
            idempotency_key=host_idempotency_key("collect", SESSION_ID),
        )
        assert collected.files[0].path == "notes/agents/t/finding.md"
        manifest = await client.set_tool_manifest(
            SESSION_ID,
            HostToolManifestRequestV1(
                manifest_sha256="c" * 64,
                tools=(
                    HostToolManifestEntryV1(
                        mcp_name="autowork_task_heartbeat",
                        broker_name="task.heartbeat",
                        schema_version="tool.v1",
                        input_schema={"type": "object"},
                        effect="read",
                        cost="none",
                        capability="task.heartbeat",
                    ),
                ),
            ),
            idempotency_key=host_idempotency_key("manifest", SESSION_ID),
        )
        assert manifest.accepted is True
        closed = await client.close_session(
            SESSION_ID,
            HostReasonRequestV1(reason="done"),
            idempotency_key=host_idempotency_key("close", SESSION_ID, "done"),
        )
        assert closed.state == "closed"
    finally:
        await client.aclose()

    assert wire.paths() == [
        ("GET", "/v1/sessions/capabilities"),
        ("POST", "/v1/sessions"),
        ("POST", f"/v1/sessions/{SESSION_ID}/start"),
        ("GET", f"/v1/sessions/{SESSION_ID}"),
        ("GET", f"/v1/sessions/{SESSION_ID}/events"),
        ("GET", f"/v1/sessions/{SESSION_ID}/logs"),
        ("POST", f"/v1/sessions/{SESSION_ID}/instructions"),
        ("POST", f"/v1/sessions/{SESSION_ID}/interrupt"),
        ("POST", f"/v1/sessions/{SESSION_ID}/resume"),
        ("POST", f"/v1/sessions/{SESSION_ID}/reconcile"),
        ("POST", f"/v1/sessions/{SESSION_ID}/collect"),
        ("POST", f"/v1/sessions/{SESSION_ID}/tool-manifest"),
        ("POST", f"/v1/sessions/{SESSION_ID}/close"),
    ]
    for request in wire.requests:
        assert request.headers["Authorization"].startswith("Bearer ")
        if request.method == "POST":
            assert _IDEMPOTENCY_KEY.fullmatch(request.headers["Idempotency-Key"])
        else:
            assert "Idempotency-Key" not in request.headers
    prepare_body = json.loads(wire.requests[1].content)
    assert prepare_body["session_id"] == str(SESSION_ID)
    assert prepare_body["auth_secret"] == _SECRET_B64
    assert set(prepare_body) == {
        "session_id",
        "adapter_kind",
        "workspace_id",
        "investigation_id",
        "task_id",
        "handoff",
        "lease_fence",
        "allowed_workspace_paths",
        "skill_names",
        "budgets",
        "model",
        "network_mode",
        "auth_secret",
    }
    assert prepare_body["network_mode"] == "isolated_egress"


async def test_wire_rejects_unknown_fields_absolute_paths_and_oversized_bodies(
    tmp_path: Path,
) -> None:
    wire = _Wire()
    wire.route(
        "GET",
        f"/v1/sessions/{SESSION_ID}",
        {**status_wire().model_dump(mode="json"), "unexpected": True},
    )
    client = _client(tmp_path, wire)
    try:
        with pytest.raises(WorkspaceProtocolError):
            await client.get_session_status(SESSION_ID)
    finally:
        await client.aclose()

    absolute = _standard_wire()
    absolute.route(
        "POST",
        f"/v1/sessions/{SESSION_ID}/collect",
        {
            "outcome": "success",
            "exit": None,
            "final_message_sha256": None,
            "files": [{"path": "/home/analyst/runtime/result.md", "sha256": "a" * 64}],
            "log_range": {"cursor_from": 0, "cursor_to": 0},
            "captured_notes": [],
            "collected_at": "2026-11-22T12:00:00Z",
        },
    )
    absolute_client = _client(tmp_path, absolute)
    try:
        with pytest.raises(WorkspaceProtocolError):
            await absolute_client.collect_results(
                SESSION_ID,
                HostCollectRequestV1(capture=True),
                idempotency_key=host_idempotency_key("collect", SESSION_ID),
            )
    finally:
        await absolute_client.aclose()

    oversized = _Wire()
    oversized.route(
        "GET",
        f"/v1/sessions/{SESSION_ID}",
        httpx.Response(
            200,
            headers={"content-length": str(HOST_RESPONSE_LIMIT + 1)},
            content=b"{}",
        ),
    )
    oversized_client = _client(tmp_path, oversized)
    try:
        with pytest.raises(WorkspaceProtocolError):
            await oversized_client.get_session_status(SESSION_ID)
    finally:
        await oversized_client.aclose()


async def test_wire_maps_session_errors_and_transport_failures(tmp_path: Path) -> None:
    conflict = _Wire()
    conflict.route(
        "GET",
        f"/v1/sessions/{SESSION_ID}",
        {"error": "session_state_conflict", "message": "busy", "retryable": False, "details": {}},
        status=409,
    )
    conflict_client = _client(tmp_path, conflict)
    try:
        with pytest.raises(HostSessionStateConflictError, match="busy"):
            await conflict_client.get_session_status(SESSION_ID)
    finally:
        await conflict_client.aclose()

    unavailable = _Wire()
    unavailable.route(
        "GET",
        f"/v1/sessions/{SESSION_ID}",
        {"error": "secret_unavailable", "message": "no secret", "retryable": False, "details": {}},
        status=503,
    )
    unavailable_client = _client(tmp_path, unavailable)
    try:
        with pytest.raises(HostSecretUnavailableError):
            await unavailable_client.get_session_status(SESSION_ID)
    finally:
        await unavailable_client.aclose()

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("socket missing", request=request)

    broken_client = _client(tmp_path, broken)
    try:
        with pytest.raises(WorkspaceLocalUnavailableError):
            await broken_client.get_session_status(SESSION_ID)
    finally:
        await broken_client.aclose()

    attempts: list[httpx.Request] = []

    def always_unauthorized(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(
            401,
            json={
                "error": "unauthorized",
                "message": "bad token",
                "retryable": False,
                "details": {},
            },
        )

    unauthorized_client = _client(tmp_path, always_unauthorized)
    try:
        with pytest.raises(WorkspaceUnauthorizedError):
            await unauthorized_client.get_session_status(SESSION_ID)
    finally:
        await unauthorized_client.aclose()
    # Reload-on-401 happens exactly once; the request is never retried forever.
    assert len(attempts) == 2


async def test_workspace_local_unavailable_fails_closed(tmp_path: Path) -> None:
    unconfigured = HttpHostSessionsClient(socket_path="", token_file=None)
    with pytest.raises(WorkspaceUnconfiguredError):
        await unconfigured.probe_capabilities("codex_cli")

    unconfigured_ports = build_agentic_operator_ports(
        Settings(
            _env_file=None,
            workspace_agent_socket="",
            workspace_agent_token_file="",
        )
    )
    assert unconfigured_ports.available is False
    assert unconfigured_ports.operator is None
    assert unconfigured_ports.secret_delivery is None

    ports = build_agentic_operator_ports(
        Settings(
            _env_file=None,
            workspace_agent_socket=str(tmp_path / "agent.sock"),
            workspace_agent_token_file=str(_token_file(tmp_path)),
        ),
        context_provider=FixedContextProvider(),
    )
    assert ports.available is True
    assert set(ports.adapters) == {
        OperatorAdapterKind.CODEX_CLI,
        OperatorAdapterKind.CLAUDE_CODE,
    }
    await ports.aclose()


async def test_secret_delivery_is_one_shot_and_never_leaks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    host = FakeHostSessionsPort()
    preparations = HostSessionPreparationRegistry()
    adapter = _codex_adapter(host, preparations)
    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli")
    )
    await adapter.prepare_session(_prepare_request(capabilities.capability_sha256))
    delivery = HostSessionSecretDelivery(host=host, preparations=preparations)

    async def deliver(secret_hex: str, key: str) -> None:
        await delivery.deliver_session_secret(
            session_id=SESSION_ID,
            investigation_id=INVESTIGATION_ID,
            task_id=TASK_ID,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            host_session_ref=str(SESSION_ID),
            secret=SessionSecret(secret_hex),
            idempotency_key=key,
        )

    await deliver(SECRET_HEX, _PREPARE_KEY)
    assert len(host.prepare_requests) == 1
    request = host.prepare_requests[0]
    assert request.auth_secret == _SECRET_B64
    assert SECRET_HEX not in repr(request)
    assert SECRET_HEX not in repr(delivery)
    assert SECRET_HEX not in caplog.text

    # An idempotent 05B1 replay re-derives the same secret and key: nothing is
    # sent a second time and the delivery stays successful.
    await deliver(SECRET_HEX, _PREPARE_KEY)
    assert len(host.prepare_requests) == 1

    with pytest.raises(HostSecretHandoffConsumedError):
        await deliver("5" * 64, _PREPARE_KEY)
    with pytest.raises(HostSecretHandoffConsumedError):
        await deliver(SECRET_HEX, "m24h0-prepare-0002")
    assert len(host.prepare_requests) == 1

    marker = preparations.delivery(SESSION_ID)
    assert marker is not None
    assert marker.secret_sha256 == hashlib.sha256(SECRET_HEX.encode("ascii")).hexdigest()
    assert SECRET_HEX not in repr(marker)
    assert SECRET_HEX not in caplog.text


async def test_secret_delivery_refuses_a_changed_capability_probe() -> None:
    host = FakeHostSessionsPort()
    preparations = HostSessionPreparationRegistry()
    adapter = _codex_adapter(host, preparations)
    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli")
    )
    await adapter.prepare_session(_prepare_request(capabilities.capability_sha256))
    host.probe = probe_wire(probe_sha256="f" * 64)
    delivery = HostSessionSecretDelivery(host=host, preparations=preparations)

    with pytest.raises(HostCapabilityUnavailableError):
        await delivery.deliver_session_secret(
            session_id=SESSION_ID,
            investigation_id=INVESTIGATION_ID,
            task_id=TASK_ID,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            host_session_ref=str(SESSION_ID),
            secret=SessionSecret(SECRET_HEX),
            idempotency_key=_PREPARE_KEY,
        )
    assert preparations.delivery(SESSION_ID) is None


def test_responses_and_requests_reject_host_paths() -> None:
    with pytest.raises(ValidationError):
        HostSessionResultV1(
            outcome="success",
            files=(HostCapturedFileV1(path="/home/analyst/result.md", sha256="a" * 64),),
            log_range=HostLogRangeV1(cursor_from=0, cursor_to=0),
            collected_at=NOW,
        )
    with pytest.raises(ValidationError):
        HostLogsPageV1(
            lines=(
                HostLogLineV1(
                    seq=1,
                    stream="stdout",
                    text="wrote /home/analyst/runtime/state.json",
                    truncated=False,
                ),
            ),
            next_cursor=1,
        )
    with pytest.raises(ValidationError):
        HostPrepareSessionRequestV1(
            session_id=SESSION_ID,
            adapter_kind="codex_cli",
            workspace_id=WORKSPACE_ID,
            investigation_id=INVESTIGATION_ID,
            task_id=TASK_ID,
            handoff=HostHandoffRefV1(handoff_id=HANDOFF_ID, version=1, sha256="b" * 64),
            lease_fence=0,
            allowed_workspace_paths=("/etc/passwd",),
            skill_names=(),
            budgets=context().budgets,
            auth_secret=_SECRET_B64,
        )


def test_infrastructure_agentic_contains_no_argv_flag_or_home_path_literals() -> None:
    backend_src = Path(__file__).resolve().parents[1] / "src" / "cti_app"
    paths = [backend_src / "application" / "agentic_host_sessions.py"]
    paths.extend(sorted((backend_src / "infrastructure" / "agentic").glob("*.py")))
    forbidden = {
        "argv flag": re.compile(r"""["']--?[A-Za-z]"""),
        "home config dir": re.compile(r"/\.(?:codex|claude)\b|~/"),
        "absolute host root": re.compile(
            r"/(?:home|root|tmp|var|usr|etc|opt|mnt|srv|run|proc|sys|dev)/"
        ),
        "process control": re.compile(r"\b(?:subprocess|Popen|shlex)\b|\bos\.exec"),
    }
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for label, pattern in forbidden.items():
            match = pattern.search(source)
            assert match is None, f"{path.name} contains a {label}: {match.group(0)!r}"


# ``sidecars.*`` is an untyped boundary for mypy (pyproject override), so the
# FakeProfile base is ``Any``; the subclass only binds the adapter kind.
class _FakeCodexProfile(FakeProfile):  # type: ignore[misc]
    """The H1 fake profile registered under the backend's adapter kind."""

    adapter_kind = "codex_cli"


def test_real_host_supervisor_contract_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    base = Path(tempfile.mkdtemp(prefix="m24lb-"))
    try:
        monkeypatch.setattr(
            supervisor_module,
            "default_profiles",
            lambda: ProfileRegistry((_FakeCodexProfile(),)),
        )
        with running_session_agent(base) as running, running.client() as client:
            workspace_id = running.ensure_workspace(client)
            settings = Settings(
                _env_file=None,
                workspace_agent_socket=str(running.socket_path),
                workspace_agent_token_file=str(running.runtime_dir / TOKEN_FILE_NAME),
            )
            ports = build_agentic_operator_ports(
                settings,
                context_provider=FixedContextProvider(context(workspace_id=workspace_id)),
                clock=lambda: NOW,
            )
            asyncio.run(_real_contract_flow(ports, running))
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def _wait_for_terminal(ports: AgenticOperatorPorts) -> None:
    host = ports.host_sessions
    assert host is not None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = await host.get_session_status(SESSION_ID)
        if status.state in {"stopped", "lost", "failed"}:
            return
        await asyncio.sleep(0.05)
    pytest.fail("the fake profile did not reach a terminal state")


async def _real_contract_flow(ports: AgenticOperatorPorts, running: RunningSessionAgent) -> None:
    operator = ports.operator
    delivery = ports.secret_delivery
    host = ports.host_sessions
    assert operator is not None and delivery is not None and host is not None
    try:
        capabilities = await operator.probe_capabilities(
            ProbeCapabilitiesRequest(
                adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli"
            )
        )
        assert capabilities.supports_non_interactive is True
        prepared = await operator.prepare_session(_prepare_request(capabilities.capability_sha256))
        assert prepared.host_session_ref == str(SESSION_ID)

        # The real prepare is the secret hand-off; only then does the session
        # runtime directory exist for the fake profile's script.
        await delivery.deliver_session_secret(
            session_id=SESSION_ID,
            investigation_id=INVESTIGATION_ID,
            task_id=TASK_ID,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            host_session_ref=prepared.host_session_ref,
            secret=SessionSecret(SECRET_HEX),
            idempotency_key=_PREPARE_KEY,
        )
        await delivery.deliver_session_secret(
            session_id=SESSION_ID,
            investigation_id=INVESTIGATION_ID,
            task_id=TASK_ID,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            host_session_ref=prepared.host_session_ref,
            secret=SessionSecret(SECRET_HEX),
            idempotency_key=_PREPARE_KEY,
        )
        write_fake_script(
            running,
            fake_script(
                [
                    {"op": "emit", "event": {"type": "started"}},
                    {"op": "write", "path": "notes/finding.md", "text": "# note\n"},
                    {"op": "write", "path": "result.md", "text": "result\n"},
                    {"op": "write", "path": "final.md", "text": "final\n"},
                    {"op": "hang"},
                ],
                final_message="real contract final",
            ),
            session_id=str(SESSION_ID),
        )

        started = await operator.start_session(
            StartSessionRequest(
                session_id=SESSION_ID,
                adapter_kind=OperatorAdapterKind.CODEX_CLI,
                host_session_ref=prepared.host_session_ref,
                capability_sha256=capabilities.capability_sha256,
            )
        )
        assert started.alive is True
        # The fake process writes its declared scratch files, then hangs.  Wait
        # for them before interrupting so collect has real captured files.
        scratch = running.session_dir() / "scratch"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (scratch / "final.md").is_file() and (scratch / "notes" / "finding.md").is_file():
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("the fake profile did not write its scratch files")

        status = await operator.get_session_status(
            SessionStatusRequest(session_id=SESSION_ID, host_session_ref=prepared.host_session_ref)
        )
        assert status.alive is True

        events = await host.get_events(SESSION_ID, after=0, limit=100)
        assert {"process_started", "state_changed"} <= {event.kind for event in events.events}
        logs = await host.get_logs(SESSION_ID, after=0, limit=100)
        assert all(not line.text.startswith("/") for line in logs.lines)

        instruction_text = "focus on the loader"
        receipt = await operator.send_instruction(
            SendInstructionRequest(
                session_id=SESSION_ID,
                host_session_ref=prepared.host_session_ref,
                instruction_id=UUID("11111111-2222-4333-8444-555555555555"),
                instruction_sha256=hashlib.sha256(instruction_text.encode()).hexdigest(),
                text=instruction_text,
                idempotency_key="m24h0-instruction-0001",
            )
        )
        assert receipt.outcome is InstructionDeliveryOutcome.ACCEPTED

        interrupted = await operator.interrupt_session(
            InterruptSessionRequest(
                session_id=SESSION_ID,
                host_session_ref=prepared.host_session_ref,
                reason="real contract interrupt",
            )
        )
        assert interrupted.stopped is True
        await _wait_for_terminal(ports)

        reconciliation = await operator.reconcile_session(
            ReconcileSessionRequest(
                session_id=SESSION_ID, host_session_ref=prepared.host_session_ref
            )
        )
        assert reconciliation.outcome is SessionReconcileOutcome.TERMINATED_CLEAN

        results = await operator.collect_results(
            CollectResultsRequest(session_id=SESSION_ID, host_session_ref=prepared.host_session_ref)
        )
        assert {item.kind for item in results.items} == {"note", "report"}
        assert all(item.summary and not item.summary.startswith("/") for item in results.items)
        assert all(item.content_sha256 for item in results.items)

        closed = await host.close_session(
            SESSION_ID,
            HostReasonRequestV1(reason="contract complete"),
            idempotency_key=host_idempotency_key("close", SESSION_ID, "contract complete"),
        )
        assert closed.state == "closed"
    finally:
        await ports.aclose()

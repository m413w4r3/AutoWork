"""M24.1 RLY-1 oracles: broker relay core, simulated transport and clock.

Every test is offline and rootless. The relay listens on a real pathname
AF_UNIX socket inside a short ``/tmp`` directory (the ``sun_path`` limit is
108 bytes), the session state is a fake implementing the injected protocol,
and the backend transport is in-process. The only real network use is the
default-transport pair, which talks to a fake loopback ``ThreadingHTTPServer``
and to a closed loopback port; no provider or AutoWork backend is contacted.

Normative source: ``docs/design/investigation-workshop/BROKER-RELAY-EGRESS.md``
§3-§4 and the RLY-1/NET-* oracle map in §7.
"""

from __future__ import annotations

import itertools
import json
import logging
import shutil
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from sidecars.workspace_agent.sessions import catalog, relay, shim

SESSION_ID = "5d3f0a6e-7c2b-4b8f-9a1d-2e4c6b8a0f13"
INVESTIGATION_ID = "3f6f2c1a-8a1e-4b7c-9d2f-1a2b3c4d5e6f"
TASK_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
OTHER_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f2a3b4c5d"
BACKEND_URL = "http://127.0.0.1:9"
IDEMPOTENCY_KEY = "m24-rly-key-0001"
SIGNATURE = "v1=" + "ab" * 32
DEFAULT_TOOLS = ("vt.file_report", "advisor.consult", "note.capture")


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeSessionState:
    """The injected local session record; ``on_manifest`` can flip liveness."""

    def __init__(
        self,
        *,
        active: bool = True,
        tools: Collection[str] | None = DEFAULT_TOOLS,
        binding: relay.SessionBinding | None = None,
    ) -> None:
        self.binding = binding or relay.SessionBinding(SESSION_ID, INVESTIGATION_ID, TASK_ID)
        self.active = active
        self.tools = None if tools is None else frozenset(tools)
        self.on_manifest: Callable[[], None] | None = None
        self.active_calls = 0
        self._lock = threading.Lock()

    @property
    def session_binding(self) -> relay.SessionBinding:
        return self.binding

    def is_active(self) -> bool:
        with self._lock:
            self.active_calls += 1
            return self.active

    def manifest_tools(self) -> Collection[str] | None:
        if self.on_manifest is not None:
            self.on_manifest()
        return self.tools


@dataclass
class RecordedCall:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    timeout: float


class RecordingTransport:
    """In-process backend transport: counts, records and can block or fail."""

    def __init__(
        self,
        response: relay.RelayResponse | None = None,
        error: BaseException | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.calls: list[RecordedCall] = []
        self.response = (
            response
            if response is not None
            else relay.RelayResponse(
                status=200, content_type="application/json", body=b'{"ok":true}'
            )
        )
        self.error = error
        self.gate = gate
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self._lock = threading.Lock()

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def cancel(self) -> None:
        self.cancelled.set()
        if self.gate is not None:
            self.gate.set()

    def send(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> relay.RelayResponse:
        with self._lock:
            self.calls.append(
                RecordedCall(
                    method=method,
                    path=path,
                    headers=dict(headers),
                    body=bytes(body),
                    timeout=timeout,
                )
            )
        self.entered.set()
        if self.gate is not None and not self.gate.wait(10):
            raise AssertionError("relay transport gate was never released")
        if self.error is not None:
            raise self.error
        return self.response


@dataclass(frozen=True)
class ParsedResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition not met within the timeout")


@pytest.fixture
def relay_dir() -> Iterator[Path]:
    """A short AF_UNIX-safe directory under ``/tmp``."""
    directory = Path(tempfile.mkdtemp(prefix="m24-rly-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def relay_factory(relay_dir: Path) -> Iterator[Callable[..., relay.BrokerRelay]]:
    created: list[relay.BrokerRelay] = []
    counter = itertools.count()

    def start(
        state: FakeSessionState | None = None,
        transport: RecordingTransport | None = None,
        *,
        limits: relay.RelayLimits | None = None,
        logger: logging.Logger | None = None,
        backend_url: str = BACKEND_URL,
    ) -> relay.BrokerRelay:
        server = relay.BrokerRelay(
            socket_path=relay_dir / f"broker-{next(counter)}.sock",
            backend_url=backend_url,
            session=state if state is not None else FakeSessionState(),
            transport=transport,
            limits=limits,
            logger=logger,
        )
        server.start()
        created.append(server)
        return server

    try:
        yield start
    finally:
        for server in created:
            server.close()


def _tool_target(*, investigation_id: str = INVESTIGATION_ID, session_id: str = SESSION_ID) -> str:
    return str(
        catalog.BROKER_TOOL_PATH.format(investigation_id=investigation_id, session_id=session_id)
    )


def _task_target(*, investigation_id: str = INVESTIGATION_ID, task_id: str = TASK_ID) -> str:
    return str(catalog.TASK_COMMAND_PATH.format(investigation_id=investigation_id, task_id=task_id))


def _handoff_target(*, investigation_id: str = INVESTIGATION_ID) -> str:
    return str(catalog.HANDOFF_PATH.format(investigation_id=investigation_id))


def _instruction_target(
    *, investigation_id: str = INVESTIGATION_ID, session_id: str = SESSION_ID
) -> str:
    return str(
        catalog.INSTRUCTIONS_PATH.format(investigation_id=investigation_id, session_id=session_id)
    )


def _signed_headers(
    *,
    session_id: str = SESSION_ID,
    key: str = IDEMPOTENCY_KEY,
    signature: str = SIGNATURE,
    timestamp: str = "1763800000",
    user_agent: str = "autowork-session-shim/1",
) -> list[tuple[str, str]]:
    return [
        ("Content-Type", "application/json"),
        ("Accept", "application/json"),
        ("User-Agent", user_agent),
        ("Idempotency-Key", key),
        ("X-AutoWork-Agent-Session", session_id),
        ("X-AutoWork-Agent-Timestamp", timestamp),
        ("X-AutoWork-Agent-Signature", signature),
    ]


def _default_headers(body: bytes) -> list[tuple[str, str]]:
    return [("Content-Length", str(len(body))), *_signed_headers()]


def _tool_body(
    tool_name: str = "vt.file_report", arguments: Mapping[str, object] | None = None
) -> bytes:
    payload = {
        "tool_name": tool_name,
        "schema_version": "1",
        "arguments": {} if arguments is None else dict(arguments),
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _tool_body_of_length(body_len: int) -> bytes:
    prefix = b'{"tool_name":"vt.file_report","schema_version":"1","arguments":{"x":"'
    suffix = b'"}}'
    return prefix + b"a" * (body_len - len(prefix) - len(suffix)) + suffix


def _task_body(command: str = "heartbeat") -> bytes:
    return json.dumps(
        {"command": command, "expected_status_version": 2}, separators=(",", ":")
    ).encode("utf-8")


def _handoff_body(*, task_id: str = TASK_ID) -> bytes:
    return json.dumps(
        {"task_id": task_id, "next_action": "continue"}, separators=(",", ":")
    ).encode("utf-8")


def _instruction_body(*, action: str = "pull", instruction_ids: object = None) -> bytes:
    payload: dict[str, object] = {"action": action}
    if instruction_ids is not None:
        payload["instruction_ids"] = instruction_ids
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _request_bytes(
    *,
    method: str,
    target: str,
    headers: Sequence[tuple[str, str]],
    body: bytes = b"",
    extra: bytes = b"",
    version: str = "HTTP/1.1",
) -> bytes:
    lines = [f"{method} {target} {version}\r\n".encode("latin-1")]
    lines.extend(f"{name}: {value}\r\n".encode("latin-1") for name, value in headers)
    lines.append(b"\r\n")
    return b"".join(lines) + body + extra


def _send(socket_path: Path, payload: bytes, *, timeout: float = 10.0) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path))
        connection.sendall(payload)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def _call(
    socket_path: Path,
    *,
    method: str = "POST",
    target: str,
    body: bytes = b"",
    headers: Sequence[tuple[str, str]] | None = None,
    extra: bytes = b"",
    version: str = "HTTP/1.1",
    timeout: float = 10.0,
) -> bytes:
    return _send(
        socket_path,
        _request_bytes(
            method=method,
            target=target,
            headers=_default_headers(body) if headers is None else headers,
            body=body,
            extra=extra,
            version=version,
        ),
        timeout=timeout,
    )


def _parse(raw: bytes) -> ParsedResponse:
    head, separator, body = raw.partition(b"\r\n\r\n")
    assert separator, raw[:200]
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode("ascii").lower()] = value.strip().decode("latin-1")
    return ParsedResponse(status=status, headers=headers, body=body)


def _error_of(raw: bytes) -> tuple[int, dict[str, object]]:
    parsed = _parse(raw)
    payload = json.loads(parsed.body)
    assert isinstance(payload, dict)
    return parsed.status, payload


# ---------------------------------------------------------------------------
# Happy path and byte-exact forwarding
# ---------------------------------------------------------------------------


def test_relay_forwards_a_valid_tool_call_once(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)
    body = (
        b'{"tool_name": "vt.file_report", "schema_version":"1", '
        b'"arguments": {"file_hash": "' + b"a" * 64 + b'"}}'
    )

    parsed = _parse(_call(server.socket_path, target=_tool_target(), body=body))

    assert parsed.status == 200
    assert parsed.body == b'{"ok":true}'
    assert transport.call_count == 1
    call = transport.calls[0]
    assert call.method == "POST"
    assert call.path == _tool_target()
    assert call.body == body
    assert call.timeout == 600.0
    assert call.headers["X-AutoWork-Agent-Signature"] == SIGNATURE
    assert call.headers["Idempotency-Key"] == IDEMPOTENCY_KEY
    assert stat.S_IMODE(server.socket_path.stat().st_mode) == 0o600


def test_relay_does_not_re_sign_and_backend_rejects_bad_hmac(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport(
        response=relay.RelayResponse(
            status=401, content_type="application/json", body=b'{"error":"invalid_signature"}'
        )
    )
    server = relay_factory(FakeSessionState(), transport)
    body = b'{"tool_name" : "vt.file_report",  "schema_version":"1", "arguments":{ }}'
    bad_signature = "v1=" + "00" * 32
    headers = [
        (name, bad_signature if name == "X-AutoWork-Agent-Signature" else value)
        for name, value in _default_headers(body)
    ]

    parsed = _parse(_call(server.socket_path, target=_tool_target(), body=body, headers=headers))

    assert parsed.status == 401
    assert parsed.body == b'{"error":"invalid_signature"}'
    assert transport.call_count == 1
    call = transport.calls[0]
    assert call.body == body
    assert call.headers["X-AutoWork-Agent-Signature"] == bad_signature


def test_replay_forwards_identical_bytes_once(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)
    body = _tool_body()

    for _ in range(2):
        assert _parse(_call(server.socket_path, target=_tool_target(), body=body)).status == 200

    assert transport.call_count == 2
    first, second = transport.calls
    assert first == second


# ---------------------------------------------------------------------------
# NET-02 / NET-03: closed operation surface, fixed backend
# ---------------------------------------------------------------------------


def test_relay_refuses_method_path_header_and_unlisted_tool(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)
    tool_body = _tool_body()
    cases: list[tuple[bytes, int, str]] = []

    cases.append(
        (
            _call(server.socket_path, method="GET", target=_tool_target()),
            403,
            "method_not_allowed",
        )
    )
    cases.append(
        (
            _call(
                server.socket_path,
                target=f"/api/investigations/{INVESTIGATION_ID}/unknown",
            ),
            403,
            "route_not_allowed",
        )
    )
    unlisted = _tool_body("shodan.host")
    cases.append(
        (
            _call(server.socket_path, target=_tool_target(), body=unlisted),
            403,
            "tool_not_in_manifest",
        )
    )
    uncatalogued = _tool_body("generated.tool")
    cases.append(
        (
            _call(server.socket_path, target=_tool_target(), body=uncatalogued),
            403,
            "tool_not_in_manifest",
        )
    )
    for name, value in (
        ("X-Forward-To", "http://10.0.0.1:8000"),
        ("Transfer-Encoding", "chunked"),
        ("Expect", "100-continue"),
        ("Upgrade", "websocket"),
    ):
        cases.append(
            (
                _call(
                    server.socket_path,
                    target=_tool_target(),
                    body=tool_body,
                    headers=[*_default_headers(tool_body), (name, value)],
                ),
                403,
                "header_not_allowed",
            )
        )
    cases.append(
        (
            _call(
                server.socket_path,
                target=_tool_target(),
                body=tool_body,
                headers=_signed_headers(),
            ),
            403,
            "content_length_required",
        )
    )

    for raw, expected_status, expected_reason in cases:
        status, payload = _error_of(raw)
        assert status == expected_status, payload
        assert payload["error"] == "relay_operation_not_allowed", payload
        assert payload["retryable"] is False
        assert cast(dict[str, object], payload["details"])["reason"] == expected_reason
    assert transport.call_count == 0


def test_relay_refuses_a_tool_missing_from_the_closed_catalogue(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(tools=("generated.tool",)), transport)

    status, payload = _error_of(
        _call(server.socket_path, target=_tool_target(), body=_tool_body("generated.tool"))
    )

    assert status == 403
    assert payload["error"] == "relay_operation_not_allowed"
    assert cast(dict[str, object], payload["details"])["reason"] == "tool_not_in_catalogue"
    assert transport.call_count == 0


def test_relay_ignores_agent_headers_and_uses_fixed_backend(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)

    assert server.backend_target == shim.parse_backend_url(BACKEND_URL)
    body = _tool_body()
    for name, value in (
        ("Host", "evil.example"),
        ("X-Backend-Url", "http://10.0.0.1:8000"),
        ("X-Forwarded-Host", "evil.example"),
    ):
        raw = _call(
            server.socket_path,
            target=_tool_target(),
            body=body,
            headers=[*_default_headers(body), (name, value)],
        )
        status, payload = _error_of(raw)
        assert status == 403
        assert payload["error"] == "relay_operation_not_allowed"
    absolute = _call(
        server.socket_path,
        target=f"http://10.0.0.1:8000{_tool_target()}",
        body=body,
    )
    status, payload = _error_of(absolute)
    assert status == 403
    assert cast(dict[str, object], payload["details"])["reason"] == "request_target_not_origin_form"
    assert transport.call_count == 0

    assert _parse(_call(server.socket_path, target=_tool_target(), body=body)).status == 200
    forwarded = transport.calls[0].headers
    assert set(forwarded) <= set(relay.FORWARDED_HEADERS)
    assert "Host" not in forwarded
    assert forwarded["X-AutoWork-Agent-Session"] == SESSION_ID


@pytest.mark.parametrize(
    "backend_url",
    [
        "http://10.0.0.1:8000",
        "https://127.0.0.1:8000",
        "http://user:secret@127.0.0.1:8000",
        "http://127.0.0.1:8000/api",
        "http://127.0.0.1:8000?x=1",
    ],
)
def test_relay_refuses_a_non_loopback_or_credentialed_backend_url(
    relay_dir: Path, backend_url: str
) -> None:
    with pytest.raises(shim.ShimConfigError):
        relay.BrokerRelay(
            socket_path=relay_dir / "broker.sock",
            backend_url=backend_url,
            session=FakeSessionState(),
            transport=RecordingTransport(),
        )


# ---------------------------------------------------------------------------
# NET-04: inactive, rechecked and revoked sessions
# ---------------------------------------------------------------------------


def test_relay_refuses_inactive_or_revoked_session(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    inactive_transport = RecordingTransport()
    inactive = relay_factory(FakeSessionState(active=False), inactive_transport)
    status, payload = _error_of(
        _call(inactive.socket_path, target=_tool_target(), body=_tool_body())
    )
    assert status == 409
    assert payload["error"] == "relay_session_inactive"
    assert inactive_transport.call_count == 0

    # The liveness recheck happens after body validation and before forwarding.
    state = FakeSessionState()
    state.on_manifest = lambda: setattr(state, "active", False)
    late_transport = RecordingTransport()
    late = relay_factory(state, late_transport)
    status, payload = _error_of(_call(late.socket_path, target=_tool_target(), body=_tool_body()))
    assert status == 409
    assert payload["error"] == "relay_session_inactive"
    assert late_transport.call_count == 0
    assert state.active_calls >= 2

    # Revocation aborts a connection waiting on the backend.
    gate = threading.Event()
    in_flight_transport = RecordingTransport(gate=gate)
    in_flight = relay_factory(FakeSessionState(), in_flight_transport)
    received: list[bytes] = []
    client = threading.Thread(
        target=lambda: received.append(
            _call(in_flight.socket_path, target=_tool_target(), body=_tool_body())
        ),
        name="relay-in-flight-client",
    )
    client.start()
    assert in_flight_transport.entered.wait(5)
    in_flight.revoke()
    client.join(5)
    assert not client.is_alive()
    assert received == [b""]
    assert in_flight_transport.call_count == 1
    gate.set()


class _CancellableSlowTransport:
    """A backend call that only returns when the relay cancels it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def send(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> relay.RelayResponse:
        del method, path, headers, body, timeout
        self.calls += 1
        self.entered.set()
        interrupted = self.cancelled.wait(10)
        self.finished.set()
        if interrupted:
            raise relay.BackendOutcomeUnknown()
        return relay.RelayResponse(status=200, content_type="application/json", body=b'{"ok":true}')

    def cancel(self) -> None:
        self.cancelled.set()


def test_revoke_cancels_the_in_flight_backend_request(
    relay_factory: Callable[..., relay.BrokerRelay],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _CancellableSlowTransport()
    server = relay_factory(FakeSessionState(), transport)  # type: ignore[arg-type]
    rejections: list[relay.RelayRejection] = []
    rejection_recorded = threading.Event()

    def record(
        self: relay.BrokerRelay, connection: socket.socket, rejection: relay.RelayRejection
    ) -> None:
        # The agent socket is aborted at the same time; record the typed
        # outcome the relay would deliver instead of racing the write.
        del self, connection
        rejections.append(rejection)
        rejection_recorded.set()

    monkeypatch.setattr(relay.BrokerRelay, "_write_error", record)
    received: list[bytes] = []
    client = threading.Thread(
        target=lambda: received.append(
            _call(server.socket_path, target=_tool_target(), body=_tool_body())
        ),
        name="relay-cancelled-client",
    )
    client.start()
    assert transport.entered.wait(5)
    server.revoke()
    client.join(5)
    assert not client.is_alive()
    assert transport.cancelled.is_set()
    # Closing the client socket can finish its thread before the relay worker
    # has unwound the cancelled backend call. Synchronize on both outcomes.
    assert transport.finished.wait(5)
    assert rejection_recorded.wait(5)
    rejection = rejections[-1]
    assert rejection.code in (
        relay.RelayErrorCode.BACKEND_OUTCOME_UNKNOWN,
        relay.RelayErrorCode.SESSION_INACTIVE,
    )
    if rejection.code is relay.RelayErrorCode.BACKEND_OUTCOME_UNKNOWN:
        assert rejection.details["outcome_unknown"] is True
    raw = received[0] if received else b""
    # A cancelled call is never reported as a successful operation.
    assert not raw.startswith(b"HTTP/1.1 200")


# ---------------------------------------------------------------------------
# NET-07: backend failure taxonomy, never retried
# ---------------------------------------------------------------------------


def test_backend_drop_after_request_returns_outcome_unknown(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport(error=relay.BackendOutcomeUnknown())
    server = relay_factory(FakeSessionState(), transport)

    status, payload = _error_of(_call(server.socket_path, target=_tool_target(), body=_tool_body()))

    assert status == 504
    assert payload["error"] == "relay_backend_outcome_unknown"
    assert payload["retryable"] is False
    assert cast(dict[str, object], payload["details"])["outcome_unknown"] is True
    assert transport.call_count == 1


def test_backend_refused_before_write_is_reported_not_sent(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport(error=relay.BackendUnreachable())
    server = relay_factory(FakeSessionState(), transport)

    status, payload = _error_of(_call(server.socket_path, target=_tool_target(), body=_tool_body()))

    assert status == 503
    assert payload["error"] == "relay_backend_unreachable"
    assert cast(dict[str, object], payload["details"])["not_sent"] is True
    assert transport.call_count == 1


@pytest.mark.parametrize(
    ("error", "status", "code", "details"),
    [
        (
            relay.BackendTimeout(outcome_unknown=True),
            504,
            "relay_timeout",
            {"outcome_unknown": True, "not_sent": False},
        ),
        (
            relay.BackendTimeout(outcome_unknown=False),
            504,
            "relay_timeout",
            {"outcome_unknown": False, "not_sent": True},
        ),
        (relay.BackendResponseTooLarge(), 502, "relay_response_too_large", {}),
    ],
)
def test_backend_failures_are_typed_and_never_retried(
    relay_factory: Callable[..., relay.BrokerRelay],
    error: BaseException,
    status: int,
    code: str,
    details: Mapping[str, object],
) -> None:
    transport = RecordingTransport(error=error)
    server = relay_factory(FakeSessionState(), transport)

    actual_status, payload = _error_of(
        _call(server.socket_path, target=_tool_target(), body=_tool_body())
    )

    assert actual_status == status
    assert payload["error"] == code
    assert payload["retryable"] is False
    actual_details = cast(dict[str, object], payload["details"])
    for name, value in details.items():
        assert actual_details[name] == value
    assert transport.call_count == 1


# ---------------------------------------------------------------------------
# NET-08: the logs never carry headers, bodies, paths or the backend URL
# ---------------------------------------------------------------------------


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_relay_log_has_no_header_body_or_url(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    handler = _RecordingHandler()
    logger = logging.getLogger("tests.workspace_agent.test_sessions_relay.no_secret")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport, logger=logger)
    user_agent = "SECRET-UA-MARKER"
    body_secret = "SECRET-BODY-MARKER"
    signature = "v1=" + "cd" * 32
    body = _tool_body(arguments={"marker": body_secret})
    headers: list[tuple[str, str]] = []
    for name, value in _default_headers(body):
        if name == "User-Agent":
            value = user_agent
        elif name == "X-AutoWork-Agent-Signature":
            value = signature
        headers.append((name, value))

    parsed = _parse(_call(server.socket_path, target=_tool_target(), body=body, headers=headers))
    assert parsed.status == 200

    text = "\n".join(handler.messages)
    for forbidden in (
        user_agent,
        body_secret,
        signature,
        BACKEND_URL,
        "127.0.0.1",
        INVESTIGATION_ID,
        TASK_ID,
        _tool_target(),
    ):
        assert forbidden not in text
    assert catalog.BROKER_TOOL_PATH in text
    assert SESSION_ID in text


# ---------------------------------------------------------------------------
# Bounds, framing and timeouts
# ---------------------------------------------------------------------------


def test_relay_limits_match_the_design_defaults() -> None:
    limits = relay.RelayLimits()
    assert relay.REQUEST_LINE_LIMIT_BYTES == 2048
    assert relay.HEADER_BLOCK_LIMIT_BYTES == 16 * 1024
    assert relay.REQUEST_BODY_LIMIT_BYTES == 1024 * 1024
    assert relay.RESPONSE_BODY_LIMIT_BYTES == 16 * 1024 * 1024
    assert relay.MAX_CONCURRENT_CONNECTIONS == 8
    assert limits.request_line_bytes == relay.REQUEST_LINE_LIMIT_BYTES
    assert limits.header_block_bytes == relay.HEADER_BLOCK_LIMIT_BYTES
    assert limits.request_body_bytes == relay.REQUEST_BODY_LIMIT_BYTES
    assert limits.response_body_bytes == relay.RESPONSE_BODY_LIMIT_BYTES
    assert limits.max_connections == relay.MAX_CONCURRENT_CONNECTIONS


def test_relay_refuses_an_oversized_request_line(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    server = relay_factory(FakeSessionState(), RecordingTransport())
    status, payload = _error_of(
        _call(server.socket_path, target="/" + "a" * (relay.REQUEST_LINE_LIMIT_BYTES + 1))
    )
    assert status == 413
    assert payload["error"] == "relay_request_too_large"


def test_relay_refuses_an_oversized_header_block(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    server = relay_factory(FakeSessionState(), RecordingTransport())
    headers = [*_default_headers(b""), ("X-Pad", "a" * relay.HEADER_BLOCK_LIMIT_BYTES)]
    status, payload = _error_of(_call(server.socket_path, target=_tool_target(), headers=headers))
    assert status == 413
    assert payload["error"] == "relay_request_too_large"


def test_relay_refuses_a_declared_body_over_the_limit(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)
    headers = [
        ("Content-Length", str(relay.REQUEST_BODY_LIMIT_BYTES + 1)),
        *_signed_headers(),
    ]
    status, payload = _error_of(
        _send(
            server.socket_path,
            _request_bytes(method="POST", target=_tool_target(), headers=headers),
        )
    )
    assert status == 413
    assert payload["error"] == "relay_request_too_large"
    assert transport.call_count == 0


def test_relay_body_bound_is_inclusive(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(
        FakeSessionState(),
        transport,
        limits=relay.RelayLimits(request_body_bytes=256),
    )
    body = _tool_body_of_length(256)
    assert _parse(_call(server.socket_path, target=_tool_target(), body=body)).status == 200
    assert transport.call_count == 1

    status, payload = _error_of(
        _send(
            server.socket_path,
            _request_bytes(
                method="POST",
                target=_tool_target(),
                headers=[("Content-Length", "257"), *_signed_headers()],
            ),
        )
    )
    assert status == 413
    assert payload["error"] == "relay_request_too_large"
    assert transport.call_count == 1


def test_relay_response_ceiling_is_enforced(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    body = b"x" * 65
    transport = RecordingTransport(
        response=relay.RelayResponse(status=200, content_type="text/plain", body=body)
    )
    server = relay_factory(
        FakeSessionState(),
        transport,
        limits=relay.RelayLimits(response_body_bytes=64),
    )

    status, payload = _error_of(_call(server.socket_path, target=_tool_target(), body=_tool_body()))

    assert status == 502
    assert payload["error"] == "relay_response_too_large"
    assert transport.call_count == 1


def test_relay_refuses_invalid_or_duplicated_content_length(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)

    for value in ("12x", ""):
        headers = [("Content-Length", value), *_signed_headers()]
        status, payload = _error_of(
            _send(
                server.socket_path,
                _request_bytes(method="POST", target=_tool_target(), headers=headers),
            )
        )
        assert status == 403, value
        assert cast(dict[str, object], payload["details"])["reason"] == "content_length_invalid"

    huge = [("Content-Length", "0" * 5000), *_signed_headers()]
    status, payload = _error_of(
        _send(
            server.socket_path,
            _request_bytes(method="POST", target=_tool_target(), headers=huge),
        )
    )
    assert status == 413
    assert payload["error"] == "relay_request_too_large"

    duplicate = [
        ("Content-Length", "0"),
        ("Content-Length", "0"),
        *_signed_headers(),
    ]
    status, payload = _error_of(
        _send(
            server.socket_path,
            _request_bytes(method="POST", target=_tool_target(), headers=duplicate),
        )
    )
    assert status == 403
    assert cast(dict[str, object], payload["details"])["reason"] == "header_duplicate"
    assert transport.call_count == 0


def test_relay_refuses_query_string_http_version_and_pipelining(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)
    body = _tool_body()

    status, payload = _error_of(
        _call(server.socket_path, target=_tool_target() + "?x=1", body=body)
    )
    assert status == 403
    assert cast(dict[str, object], payload["details"])["reason"] == "query_string_refused"

    status, payload = _error_of(
        _call(server.socket_path, target=_tool_target(), body=body, version="HTTP/1.0")
    )
    assert status == 403
    assert cast(dict[str, object], payload["details"])["reason"] == "http_version_not_allowed"

    first = _request_bytes(
        method="POST", target=_tool_target(), headers=_default_headers(body), body=body
    )
    second = _request_bytes(
        method="POST", target=_tool_target(), headers=_default_headers(body), body=body
    )
    status, payload = _error_of(_send(server.socket_path, first + second))
    assert status == 403
    assert cast(dict[str, object], payload["details"])["reason"] == "pipelining_refused"
    assert transport.call_count == 0


def test_relay_refuses_a_non_json_body(relay_factory: Callable[..., relay.BrokerRelay]) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)

    status, payload = _error_of(_call(server.socket_path, target=_tool_target(), body=b"not json"))

    assert status == 403
    assert cast(dict[str, object], payload["details"])["reason"] == "body_invalid_json"
    assert transport.call_count == 0


def test_relay_times_out_an_incomplete_request(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    server = relay_factory(
        FakeSessionState(),
        RecordingTransport(),
        limits=relay.RelayLimits(request_read_timeout=0.1),
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5.0)
        connection.connect(str(server.socket_path))
        connection.sendall(b"POST /api/investigations/partial")
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)

    status, payload = _error_of(b"".join(chunks))
    assert status == 504
    assert payload["error"] == "relay_timeout"
    assert cast(dict[str, object], payload["details"])["not_sent"] is True


def test_response_write_timeout_after_forward_is_outcome_unknown(
    relay_factory: Callable[..., relay.BrokerRelay],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"x" * (4 * 1024 * 1024)
    transport = RecordingTransport(
        response=relay.RelayResponse(status=200, content_type="application/octet-stream", body=body)
    )
    recorded: list[relay.RelayRejection] = []

    def record(
        self: relay.BrokerRelay, connection: socket.socket, rejection: relay.RelayRejection
    ) -> None:
        del self, connection
        recorded.append(rejection)

    monkeypatch.setattr(relay.BrokerRelay, "_write_error", record)
    server = relay_factory(
        FakeSessionState(),
        transport,
        limits=relay.RelayLimits(request_read_timeout=0.1),
    )
    tool_body = _tool_body()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5.0)
        connection.connect(str(server.socket_path))
        connection.sendall(
            _request_bytes(
                method="POST",
                target=_tool_target(),
                headers=_default_headers(tool_body),
                body=tool_body,
            )
        )
        # Never read the 4 MiB response: the relay's write must time out.
        _wait_until(lambda: bool(recorded), timeout=10.0)
    assert transport.call_count == 1
    rejection = recorded[-1]
    assert rejection.code is relay.RelayErrorCode.TIMEOUT
    assert rejection.details == {"outcome_unknown": True, "not_sent": False}


# ---------------------------------------------------------------------------
# Binding, manifest, commands and per-tool timeouts
# ---------------------------------------------------------------------------


def test_relay_refuses_a_broker_call_before_an_accepted_manifest(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(tools=None), transport)

    status, payload = _error_of(_call(server.socket_path, target=_tool_target(), body=_tool_body()))

    assert status == 409
    assert payload["error"] == "relay_manifest_missing"
    assert transport.call_count == 0


def test_relay_binds_every_route_to_the_session_record(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)
    missing_session_header = [
        (name, value) for name, value in _signed_headers() if name != "X-AutoWork-Agent-Session"
    ]
    without_key = [(name, value) for name, value in _signed_headers() if name != "Idempotency-Key"]
    bad_key = [
        (name, "bad key!" if name == "Idempotency-Key" else value)
        for name, value in _signed_headers()
    ]
    cases: list[tuple[str, bytes, Sequence[tuple[str, str]], int, str]] = [
        (
            _tool_target(investigation_id=OTHER_ID),
            _tool_body(),
            _signed_headers(),
            403,
            "relay_binding_mismatch",
        ),
        (
            _tool_target(session_id=OTHER_ID),
            _tool_body(),
            _signed_headers(),
            403,
            "relay_binding_mismatch",
        ),
        (
            _task_target(task_id=OTHER_ID),
            _task_body(),
            _signed_headers(),
            403,
            "relay_binding_mismatch",
        ),
        (
            _handoff_target(),
            _handoff_body(task_id=OTHER_ID),
            _signed_headers(),
            403,
            "relay_binding_mismatch",
        ),
        (
            _tool_target(),
            _tool_body(),
            missing_session_header,
            403,
            "relay_binding_mismatch",
        ),
        (_tool_target(), _tool_body(), without_key, 403, "relay_operation_not_allowed"),
        (_tool_target(), _tool_body(), bad_key, 403, "relay_operation_not_allowed"),
    ]

    for target, body, headers, expected_status, expected_code in cases:
        raw = _call(
            server.socket_path,
            target=target,
            body=body,
            headers=[("Content-Length", str(len(body))), *headers],
        )
        status, payload = _error_of(raw)
        assert status == expected_status, payload
        assert payload["error"] == expected_code, payload
    assert transport.call_count == 0


def test_relay_forwards_task_commands_and_handoffs(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)

    for command in ("heartbeat", "submit"):
        body = _task_body(command)
        assert _parse(_call(server.socket_path, target=_task_target(), body=body)).status == 200
    handoff = _handoff_body()
    assert _parse(_call(server.socket_path, target=_handoff_target(), body=handoff)).status == 200
    assert [call.timeout for call in transport.calls] == [120.0, 120.0, 120.0]

    refused = _task_body("interrupt")
    status, payload = _error_of(_call(server.socket_path, target=_task_target(), body=refused))
    assert status == 403
    assert cast(dict[str, object], payload["details"])["reason"] == "command_not_allowed"
    assert transport.call_count == 3


def test_relay_forwards_the_operator_instruction_actions_byte_exact(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    # The instructions route is not a broker tool: no manifest is required.
    server = relay_factory(FakeSessionState(tools=None), transport)
    instruction_ids = [
        "12345678-1234-4abc-8def-1234567890ab",
        "abcdef12-3456-4abc-9def-abcdef123456",
    ]
    pull = _instruction_body()
    ack = _instruction_body(action="ack", instruction_ids=instruction_ids)

    assert _parse(_call(server.socket_path, target=_instruction_target(), body=pull)).status == 200
    assert _parse(_call(server.socket_path, target=_instruction_target(), body=ack)).status == 200

    assert transport.call_count == 2
    assert [call.path for call in transport.calls] == [
        _instruction_target(),
        _instruction_target(),
    ]
    assert [call.body for call in transport.calls] == [pull, ack]
    assert [call.timeout for call in transport.calls] == [120.0, 120.0]


@pytest.mark.parametrize(
    "body",
    [
        _instruction_body(action="enqueue"),
        _instruction_body(action="cancel"),
        _instruction_body(action="deliver"),
        _instruction_body(action="pull", instruction_ids=[]),
        _instruction_body(
            action="ack",
            instruction_ids=[f"{index:08x}-1234-4abc-8def-1234567890ab" for index in range(33)],
        ),
        _instruction_body(action="ack", instruction_ids=["not-a-uuid"]),
        _instruction_body(
            action="ack",
            instruction_ids=[
                "12345678-1234-4abc-8def-1234567890ab",
                "12345678-1234-4abc-8def-1234567890ab",
            ],
        ),
        _instruction_body(action="ack", instruction_ids="12345678-1234-4abc-8def-1234567890ab"),
        _instruction_body(action="ack"),
        b'{"action":"pull","tool_name":"vt.file_report"}',
        b'{"action":"ack","instruction_ids":["12345678-1234-4abc-8def-1234567890ab"],"x":1}',
        b"[]",
    ],
)
def test_relay_refuses_human_or_malformed_instruction_bodies(
    relay_factory: Callable[..., relay.BrokerRelay], body: bytes
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)

    status, payload = _error_of(_call(server.socket_path, target=_instruction_target(), body=body))

    assert status == 403
    assert payload["error"] == "relay_operation_not_allowed"
    assert transport.call_count == 0


def test_relay_bounds_the_instruction_body(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)
    body = json.dumps(
        {"action": "pull", "pad": "a" * relay.INSTRUCTION_BODY_LIMIT_BYTES},
        separators=(",", ":"),
    ).encode("utf-8")

    status, payload = _error_of(_call(server.socket_path, target=_instruction_target(), body=body))

    assert status == 413
    assert payload["error"] == "relay_request_too_large"
    assert transport.call_count == 0


def test_relay_binds_the_instruction_route_to_the_session(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(), transport)
    pull = _instruction_body()

    for target in (
        _instruction_target(investigation_id=OTHER_ID),
        _instruction_target(session_id=OTHER_ID),
    ):
        status, payload = _error_of(_call(server.socket_path, target=target, body=pull))
        assert status == 403
        assert payload["error"] == "relay_binding_mismatch"
    assert transport.call_count == 0


@pytest.mark.parametrize(
    ("tool_name", "timeout"),
    [
        ("advisor.consult", 1800.0),
        ("vt.file_report", 600.0),
        ("analysis.run_script", 600.0),
        ("note.capture", 120.0),
    ],
)
def test_relay_uses_the_catalogue_timeout(
    relay_factory: Callable[..., relay.BrokerRelay], tool_name: str, timeout: float
) -> None:
    transport = RecordingTransport()
    server = relay_factory(FakeSessionState(tools=(tool_name,)), transport)

    assert (
        _parse(_call(server.socket_path, target=_tool_target(), body=_tool_body(tool_name))).status
        == 200
    )
    assert transport.calls[0].timeout == timeout


# ---------------------------------------------------------------------------
# Concurrency and socket lifecycle
# ---------------------------------------------------------------------------


def test_relay_caps_concurrent_connections(relay_factory: Callable[..., relay.BrokerRelay]) -> None:
    gate = threading.Event()
    transport = RecordingTransport(gate=gate)
    server = relay_factory(FakeSessionState(), transport)
    results: list[bytes] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def client() -> None:
        try:
            raw = _call(server.socket_path, target=_tool_target(), body=_tool_body())
        except BaseException as exc:  # pragma: no cover - reported below
            with lock:
                failures.append(exc)
            return
        with lock:
            results.append(raw)

    workers = [threading.Thread(target=client, name=f"relay-client-{index}") for index in range(8)]
    for worker in workers:
        worker.start()
    _wait_until(lambda: transport.call_count == 8)
    assert server.active_connections == 8

    ninth = threading.Thread(target=client, name="relay-client-9")
    ninth.start()
    time.sleep(0.2)
    assert len(results) == 0
    assert transport.call_count == 8

    gate.set()
    for worker in [*workers, ninth]:
        worker.join(10)
        assert not worker.is_alive()
    assert failures == []
    assert len(results) == 9
    assert transport.call_count == 9
    assert all(_parse(raw).status == 200 for raw in results)


def test_relay_socket_is_private_and_removed_on_close(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    server = relay_factory(FakeSessionState(), RecordingTransport())
    path = server.socket_path
    assert stat.S_ISSOCK(path.lstat().st_mode)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    server.close()

    assert not path.exists()
    server.close()
    assert not path.exists()


def test_relay_refuses_an_over_long_socket_path(relay_dir: Path) -> None:
    long_path = relay_dir / ("x" * 120) / "broker.sock"
    with pytest.raises(relay.RelayConfigError) as excinfo:
        relay.BrokerRelay(
            socket_path=long_path,
            backend_url=BACKEND_URL,
            session=FakeSessionState(),
            transport=RecordingTransport(),
        )
    assert excinfo.value.code == "socket_path_too_long"


def test_relay_refuses_to_replace_a_non_socket_path(relay_dir: Path) -> None:
    path = relay_dir / "broker.sock"
    path.write_text("not a socket", encoding="utf-8")
    server = relay.BrokerRelay(
        socket_path=path,
        backend_url=BACKEND_URL,
        session=FakeSessionState(),
        transport=RecordingTransport(),
    )

    with pytest.raises(relay.RelayConfigError) as excinfo:
        server.start()

    assert excinfo.value.code == "socket_path_exists"
    assert path.read_text(encoding="utf-8") == "not a socket"


# ---------------------------------------------------------------------------
# Default transport against a fake loopback backend (still offline)
# ---------------------------------------------------------------------------


class _FakeBackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        backend = cast(_FakeBackend, self.server)
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        with backend.lock:
            backend.requests.append(
                {"path": self.path, "headers": dict(self.headers), "body": body}
            )
        if backend.delay:
            time.sleep(backend.delay)
        self.send_response(backend.status)
        self.send_header("Content-Type", backend.content_type)
        if backend.location is not None:
            self.send_header("Location", backend.location)
        self.send_header("Content-Length", str(len(backend.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(backend.body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _FakeBackend(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeBackendHandler)
        self.requests: list[dict[str, object]] = []
        self.status = 200
        self.content_type = "application/json"
        self.body = b'{"ok":true}'
        self.location: str | None = None
        self.delay = 0.0
        self.lock = threading.Lock()


@pytest.fixture
def fake_backend() -> Iterator[_FakeBackend]:
    backend = _FakeBackend()
    thread = threading.Thread(target=backend.serve_forever, name="relay-fake-backend", daemon=True)
    thread.start()
    try:
        yield backend
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join(5)


def test_default_transport_forwards_byte_exact_to_the_fixed_loopback(
    relay_factory: Callable[..., relay.BrokerRelay], fake_backend: _FakeBackend
) -> None:
    host, port = fake_backend.server_address[:2]
    assert host == "127.0.0.1"
    server = relay_factory(FakeSessionState(), None, backend_url=f"http://{host}:{port}")
    body = b'{"tool_name": "vt.file_report", "schema_version":"1", "arguments":{ }}'

    parsed = _parse(_call(server.socket_path, target=_tool_target(), body=body))

    assert parsed.status == 200
    assert parsed.body == b'{"ok":true}'
    assert len(fake_backend.requests) == 1
    received = fake_backend.requests[0]
    assert received["path"] == _tool_target()
    assert received["body"] == body
    headers = cast(dict[str, str], received["headers"])
    assert headers["X-AutoWork-Agent-Signature"] == SIGNATURE
    assert headers["Idempotency-Key"] == IDEMPOTENCY_KEY


def test_default_transport_never_follows_a_redirect(
    relay_factory: Callable[..., relay.BrokerRelay], fake_backend: _FakeBackend
) -> None:
    fake_backend.status = 302
    fake_backend.body = b""
    fake_backend.content_type = "text/plain"
    fake_backend.location = "http://127.0.0.1:9/elsewhere"
    host, port = fake_backend.server_address[:2]
    assert isinstance(host, str)
    server = relay_factory(FakeSessionState(), None, backend_url=f"http://{host}:{port}")

    parsed = _parse(_call(server.socket_path, target=_tool_target(), body=_tool_body()))

    assert parsed.status == 302
    assert parsed.body == b""
    assert len(fake_backend.requests) == 1


def test_default_transport_reports_an_unreachable_backend(
    relay_factory: Callable[..., relay.BrokerRelay],
) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    finally:
        probe.close()
    server = relay_factory(FakeSessionState(), None, backend_url=f"http://127.0.0.1:{port}")

    status, payload = _error_of(_call(server.socket_path, target=_tool_target(), body=_tool_body()))

    assert status == 503
    assert payload["error"] == "relay_backend_unreachable"
    assert cast(dict[str, object], payload["details"])["not_sent"] is True


def test_default_transport_cancel_aborts_the_in_flight_request(
    fake_backend: _FakeBackend,
) -> None:
    fake_backend.delay = 2.0
    host, port = fake_backend.server_address[:2]
    target = shim.BackendTarget(host=cast(str, host), port=int(port))
    transport = relay.HttpBackendTransport(target)
    outcome: list[BaseException | None] = []

    def call() -> None:
        try:
            transport.send(method="POST", path="/api/x", headers={}, body=b"{}", timeout=5.0)
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=call, name="relay-cancelled-send")
    worker.start()
    _wait_until(lambda: len(fake_backend.requests) == 1)
    started = time.monotonic()
    transport.cancel()
    worker.join(5)
    assert not worker.is_alive()
    assert time.monotonic() - started < 1.0
    assert outcome and isinstance(outcome[0], relay.BackendTransportError)

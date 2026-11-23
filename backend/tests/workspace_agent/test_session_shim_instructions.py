"""M24.1 ID-6 oracles: the two fixed instruction tools end to end.

Every test is offline and rootless. The real shim subprocess (MCP stdio) talks
to a real :class:`relay.BrokerRelay` on an ``/tmp`` AF_UNIX path; the relay's
backend transport is an in-process recorder, so the exact bytes, headers,
signature and timeout are observable without any network. Raw relay calls
cover the operations the shim must never emit (human ``enqueue``/``cancel``
and malformed ack bodies).

Normative sources: ``docs/design/investigation-workshop/INSTRUCTION-DELIVERY.md``
§3.6 and ``docs/design/investigation-workshop/BROKER-RELAY-EGRESS.md`` §4.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from sidecars.workspace_agent.sessions import catalog, relay

from tests.workspace_agent import session_shim_support as support

INSTRUCTION_PATH = catalog.INSTRUCTIONS_PATH.format(
    investigation_id=support.INVESTIGATION_ID, session_id=support.SESSION_ID
)
IDEMPOTENCY_KEY = "m24-id6-key-0001"
SIGNATURE = "v1=" + "ab" * 32
FIRST_ID = "12345678-1234-4abc-8def-1234567890ab"
SECOND_ID = "abcdef12-3456-4abc-9def-abcdef123456"
OTHER_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f2a3b4c5d"
EXPECTED_HEADERS = {
    "Accept",
    "Content-Type",
    "User-Agent",
    "Content-Length",
    "Idempotency-Key",
    "X-AutoWork-Agent-Session",
    "X-AutoWork-Agent-Timestamp",
    "X-AutoWork-Agent-Signature",
}
DELIVERED_INSTRUCTIONS = [
    {
        "id": FIRST_ID,
        "investigation_id": support.INVESTIGATION_ID,
        "task_id": support.TASK_ID,
        "session_id": support.SESSION_ID,
        "state": "delivered",
        "text_sha256": "a" * 64,
        "delivery_sha256": "b" * 64,
        "delivery_mode": "next_run",
        "delivered_text": "Pivote sur le domaine synthétique.",
        "delivery_note_diffs": [],
        "queued_at": "2026-11-23T10:00:00+00:00",
        "delivered_at": "2026-11-23T10:01:00+00:00",
    }
]

PULL_ARGUMENTS = {"investigation_id": support.INVESTIGATION_ID}
ACK_ARGUMENTS = {
    "investigation_id": support.INVESTIGATION_ID,
    "instruction_ids": [FIRST_ID, SECOND_ID],
}


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeSessionState:
    """The relay's injected local session record; no accepted manifest."""

    def __init__(
        self,
        *,
        active: bool = True,
        tools: Collection[str] | None = None,
        binding: relay.SessionBinding | None = None,
    ) -> None:
        self.binding = binding or relay.SessionBinding(
            support.SESSION_ID, support.INVESTIGATION_ID, support.TASK_ID
        )
        self.active = active
        self.tools = None if tools is None else frozenset(tools)

    @property
    def session_binding(self) -> relay.SessionBinding:
        return self.binding

    def is_active(self) -> bool:
        return self.active

    def manifest_tools(self) -> Collection[str] | None:
        return self.tools


@dataclass(frozen=True)
class BackendCall:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    timeout: float


class RecordingTransport:
    """In-process relay transport recording every forwarded request."""

    def __init__(
        self,
        response: relay.RelayResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.calls: list[BackendCall] = []
        self.response = (
            response
            if response is not None
            else relay.RelayResponse(
                status=200, content_type="application/json", body=b'{"ok":true}'
            )
        )
        self.error = error
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)

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
                BackendCall(
                    method=method,
                    path=path,
                    headers=dict(headers),
                    body=bytes(body),
                    timeout=timeout,
                )
            )
        if self.error is not None:
            raise self.error
        return self.response

    def cancel(self) -> None:
        """The relay cancels in-flight calls on revoke; this fake finishes fast."""
        return None


class DroppingFakeRelay:
    """A UDS listener that records exactly one full request, then hangs up."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="m24-id6-fake-", dir="/tmp"))
        self.path = self.base / "relay.sock"
        self.connections = 0
        self.requests: list[bytes] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.path))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, name="fake-relay", daemon=True)
        self._thread.start()

    def __enter__(self) -> DroppingFakeRelay:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._stopping.set()
        self._thread.join(timeout=5)
        try:
            self._listener.close()
        except OSError:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self.connections += 1
            try:
                request = _read_full_request(connection)
                with self._lock:
                    self.requests.append(request)
            except OSError:
                pass
            finally:
                connection.close()


def _read_full_request(connection: socket.socket) -> bytes:
    connection.settimeout(10.0)
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = connection.recv(65536)
        if not chunk:
            return bytes(buffer)
        buffer.extend(chunk)
    head, separator, rest = bytes(buffer).partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(rest) < length:
        chunk = connection.recv(65536)
        if not chunk:
            break
        rest += chunk
    return head + separator + rest


@pytest.fixture
def relay_dir() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="m24-id6-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def relays() -> Iterator[list[relay.BrokerRelay]]:
    created: list[relay.BrokerRelay] = []
    try:
        yield created
    finally:
        for server in created:
            server.close()


def _start_relay(
    directory: Path,
    created: list[relay.BrokerRelay],
    transport: RecordingTransport,
    state: FakeSessionState | None = None,
) -> relay.BrokerRelay:
    server = relay.BrokerRelay(
        socket_path=directory / "broker.sock",
        backend_url="http://127.0.0.1:9",
        session=state if state is not None else FakeSessionState(),
        transport=transport,
    )
    server.start()
    created.append(server)
    return server


def _relay_harness(broker_socket: Path) -> support.ShimHarness:
    """The existing shim harness, with the production relay socket only."""
    harness = support.ShimHarness(backend_url="http://127.0.0.1:1")
    environment = support.shim_environment(
        base=harness.base,
        socket_path=harness.socket_path,
        backend_url="http://127.0.0.1:1",
    )
    del environment["AUTOWORK_BACKEND_URL"]
    environment["AUTOWORK_BROKER_SOCKET"] = str(broker_socket)
    harness.environment = environment
    return harness


def _envelope(response: Mapping[str, object]) -> tuple[dict[str, object], bool]:
    result = cast(dict[str, object], response["result"])
    content = cast(list[dict[str, object]], result["content"])
    envelope = json.loads(cast(str, content[0]["text"]))
    assert isinstance(envelope, dict), envelope
    return cast(dict[str, object], envelope), bool(result["isError"])


def _request_bytes(
    *,
    method: str,
    target: str,
    headers: Sequence[tuple[str, str]],
    body: bytes = b"",
) -> bytes:
    lines = [f"{method} {target} HTTP/1.1\r\n".encode("latin-1")]
    lines.extend(f"{name}: {value}\r\n".encode("latin-1") for name, value in headers)
    lines.append(b"\r\n")
    return b"".join(lines) + body


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


def _raw_call(socket_path: Path, *, target: str, body: bytes) -> bytes:
    headers = [
        ("Content-Type", "application/json"),
        ("Accept", "application/json"),
        ("User-Agent", "autowork-session-shim/1"),
        ("Content-Length", str(len(body))),
        ("Idempotency-Key", IDEMPOTENCY_KEY),
        ("X-AutoWork-Agent-Session", support.SESSION_ID),
        ("X-AutoWork-Agent-Timestamp", "1763800000"),
        ("X-AutoWork-Agent-Signature", SIGNATURE),
    ]
    return _send(
        socket_path,
        _request_bytes(method="POST", target=target, headers=headers, body=body),
    )


def _error_of(raw: bytes) -> tuple[int, dict[str, object]]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    assert separator, raw[:200]
    status = int(head.split(b"\r\n")[0].split(b" ")[1])
    payload = json.loads(body)
    assert isinstance(payload, dict)
    return status, payload


def _json_body(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Shim -> relay -> backend: the two operator actions
# ---------------------------------------------------------------------------


def test_pending_forwards_the_fixed_pull_and_returns_delivered_instructions(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = RecordingTransport(
        response=relay.RelayResponse(
            status=200,
            content_type="application/json",
            body=support.canonical_json(DELIVERED_INSTRUCTIONS),
        )
    )
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        envelope, is_error = _envelope(
            shim_process.call_tool("autowork_instruction_pending", PULL_ARGUMENTS)
        )

    assert is_error is False
    assert envelope["status"] == 200
    assert envelope["data"] == DELIVERED_INSTRUCTIONS
    assert envelope["data_not_instructions"] is True
    assert backend.count == 1
    call = backend.calls[0]
    assert call.method == "POST"
    assert call.path == INSTRUCTION_PATH
    assert call.body == support.canonical_json({"action": "pull"})
    assert call.timeout == 120.0
    assert set(call.headers) == EXPECTED_HEADERS
    assert call.headers["Content-Length"] == str(len(call.body))
    assert call.headers["X-AutoWork-Agent-Session"] == support.SESSION_ID
    assert call.headers["Idempotency-Key"] == support.expected_client_key(
        support.SESSION_ID, "instruction.pending", PULL_ARGUMENTS
    )
    assert call.headers["X-AutoWork-Agent-Signature"] == support.reference_signature(
        support.SECRET,
        method="POST",
        path=call.path,
        session_id=support.SESSION_ID,
        body=call.body,
        idempotency_key=call.headers["Idempotency-Key"],
        timestamp=int(call.headers["X-AutoWork-Agent-Timestamp"]),
    )


def test_ack_forwards_exactly_the_ids_and_a_deliberate_replay_is_identical(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = RecordingTransport(
        response=relay.RelayResponse(
            status=200,
            content_type="application/json",
            body=support.canonical_json(DELIVERED_INSTRUCTIONS),
        )
    )
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        first, first_error = _envelope(
            shim_process.call_tool("autowork_instruction_ack", ACK_ARGUMENTS)
        )
        second, second_error = _envelope(
            shim_process.call_tool("autowork_instruction_ack", ACK_ARGUMENTS)
        )

    assert first_error is False and second_error is False
    assert first["data"] == second["data"] == DELIVERED_INSTRUCTIONS
    assert backend.count == 2
    expected_body = support.canonical_json(
        {"action": "ack", "instruction_ids": [FIRST_ID, SECOND_ID]}
    )
    expected_key = support.expected_client_key(support.SESSION_ID, "instruction.ack", ACK_ARGUMENTS)
    assert [call.path for call in backend.calls] == [INSTRUCTION_PATH, INSTRUCTION_PATH]
    assert [call.body for call in backend.calls] == [expected_body, expected_body]
    assert [call.timeout for call in backend.calls] == [120.0, 120.0]
    assert [call.headers["Idempotency-Key"] for call in backend.calls] == [
        expected_key,
        expected_key,
    ]
    for call in backend.calls:
        assert call.headers["X-AutoWork-Agent-Signature"] == support.reference_signature(
            support.SECRET,
            method="POST",
            path=call.path,
            session_id=support.SESSION_ID,
            body=call.body,
            idempotency_key=call.headers["Idempotency-Key"],
            timestamp=int(call.headers["X-AutoWork-Agent-Timestamp"]),
        )


def test_instruction_route_needs_no_accepted_broker_manifest(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = RecordingTransport()
    server = _start_relay(relay_dir, relays, backend, FakeSessionState(tools=None))

    raw = _raw_call(server.socket_path, target=INSTRUCTION_PATH, body=b'{"action":"pull"}')

    assert _error_of(raw)[0] == 200
    assert backend.count == 1
    assert backend.calls[0].body == b'{"action":"pull"}'


# ---------------------------------------------------------------------------
# Human-only actions and malformed bodies never reach the backend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "enqueue", "text": "do more"},
        {"action": "cancel", "instruction_id": FIRST_ID, "reason": "changed my mind"},
        {"action": "pull", "tool_name": "vt.file_report"},
        {"action": "pull", "instruction_ids": [FIRST_ID]},
        {"action": "ack"},
        {"action": "ack", "instruction_ids": []},
        {"action": "ack", "instruction_ids": [FIRST_ID] * 33},
        {"action": "ack", "instruction_ids": ["not-a-uuid"]},
        {"action": "ack", "instruction_ids": [FIRST_ID, FIRST_ID]},
        {"action": "ack", "instruction_ids": FIRST_ID},
        {"action": "ack", "instruction_ids": [FIRST_ID], "text": "extra"},
        {"action": "deliver"},
        ["pull"],
    ],
)
def test_relay_refuses_human_or_malformed_instruction_operations(
    relay_dir: Path, relays: list[relay.BrokerRelay], payload: object
) -> None:
    backend = RecordingTransport()
    server = _start_relay(relay_dir, relays, backend)

    status, error = _error_of(
        _raw_call(server.socket_path, target=INSTRUCTION_PATH, body=_json_body(payload))
    )

    assert status == 403
    assert error["error"] == "relay_operation_not_allowed"
    assert backend.count == 0


def test_relay_refuses_an_oversized_instruction_body(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = RecordingTransport()
    server = _start_relay(relay_dir, relays, backend)
    body = _json_body({"action": "pull", "pad": "a" * relay.INSTRUCTION_BODY_LIMIT_BYTES})

    status, error = _error_of(_raw_call(server.socket_path, target=INSTRUCTION_PATH, body=body))

    assert status == 413
    assert error["error"] == "relay_request_too_large"
    assert backend.count == 0


def test_relay_binds_the_instruction_route_to_the_session(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = RecordingTransport()
    server = _start_relay(relay_dir, relays, backend)
    other_investigation = catalog.INSTRUCTIONS_PATH.format(
        investigation_id=OTHER_ID, session_id=support.SESSION_ID
    )
    other_session = catalog.INSTRUCTIONS_PATH.format(
        investigation_id=support.INVESTIGATION_ID, session_id=OTHER_ID
    )

    for target in (other_investigation, other_session):
        status, error = _error_of(
            _raw_call(server.socket_path, target=target, body=b'{"action":"pull"}')
        )
        assert status == 403
        assert error["error"] == "relay_binding_mismatch"
    assert backend.count == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"investigation_id": support.INVESTIGATION_ID, "instruction_ids": []},
        {"investigation_id": support.INVESTIGATION_ID, "instruction_ids": [FIRST_ID] * 33},
        {"investigation_id": support.INVESTIGATION_ID, "instruction_ids": ["not-a-uuid"]},
        {"investigation_id": support.INVESTIGATION_ID, "instruction_ids": [FIRST_ID, FIRST_ID]},
        {"investigation_id": support.INVESTIGATION_ID, "url": "http://example.invalid/"},
    ],
)
def test_shim_refuses_invalid_ack_arguments_without_network(
    relay_dir: Path, relays: list[relay.BrokerRelay], arguments: dict[str, object]
) -> None:
    backend = RecordingTransport()
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        response = shim_process.call_tool("autowork_instruction_ack", arguments)

    error = cast(dict[str, object], response["error"])
    assert error["code"] == -32602
    assert cast(dict[str, object], error["data"])["code"] == "invalid_arguments"
    assert backend.count == 0


# ---------------------------------------------------------------------------
# Ambiguity: never replayed automatically
# ---------------------------------------------------------------------------


def test_ambiguous_ack_is_outcome_unknown_and_not_retried(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = RecordingTransport(error=relay.BackendOutcomeUnknown())
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        envelope, is_error = _envelope(
            shim_process.call_tool("autowork_instruction_ack", ACK_ARGUMENTS)
        )

    assert is_error is True
    assert envelope["error"] == "backend_outcome_unknown"
    assert envelope["outcome"] == "outcome_unknown"
    assert envelope["relay_error"] == "relay_backend_outcome_unknown"
    assert envelope["retryable"] is False
    # A deliberate identical replay is safe: the backend returns the same row.
    assert envelope["idempotent_replay_allowed"] is True
    assert envelope["data"] is None
    assert backend.count == 1


def test_dropped_relay_after_the_write_is_outcome_unknown_without_replay() -> None:
    with DroppingFakeRelay() as fake:
        with _relay_harness(fake.path) as shim_process:
            shim_process.initialize()
            envelope, is_error = _envelope(
                shim_process.call_tool("autowork_instruction_ack", ACK_ARGUMENTS)
            )

        assert is_error is True
        assert envelope["error"] == "backend_outcome_unknown"
        assert envelope["outcome"] == "outcome_unknown"
        assert envelope["retryable"] is False
        assert envelope["idempotent_replay_allowed"] is True
        assert fake.connections == 1
        assert len(fake.requests) == 1
        head, _, body = fake.requests[0].partition(b"\r\n\r\n")
        assert head.split(b"\r\n")[0] == f"POST {INSTRUCTION_PATH} HTTP/1.1".encode("ascii")
        assert json.loads(body) == {
            "action": "ack",
            "instruction_ids": [FIRST_ID, SECOND_ID],
        }

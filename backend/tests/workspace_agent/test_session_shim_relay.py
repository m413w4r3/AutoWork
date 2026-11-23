"""M24.1 RLY-2 oracles: shim AF_UNIX relay transport and ambiguity mapping.

Every test is offline and rootless. A real :class:`relay.BrokerRelay` (RLY-1)
listens on a short ``/tmp`` pathname AF_UNIX socket in front of an in-process
fake backend that counts every request; the real shim subprocess is driven
through its MCP stdio entry point with ``AUTOWORK_BROKER_SOCKET``. Two
shim-side failures (connect refused, relay drops after the write) use a fake
relay socket instead, so ``not_sent``/``outcome_unknown`` is proven for the
shim's own transport leg too.

Normative sources: ``docs/design/investigation-workshop/BROKER-RELAY-EGRESS.md``
§3-§4 and the RLY-2 row of §7 (NET-01 simulated, NET-03, NET-05..NET-08).
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from sidecars.workspace_agent.sessions import catalog, relay, shim

from tests.workspace_agent import session_shim_support as support

MANIFEST_TOOLS = ("advisor.consult", "note.capture", "vt.file_report")
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
CONSULT_ARGUMENTS = {
    "investigation_id": support.INVESTIGATION_ID,
    "consultation_type": "interpretation",
    "question": "Synthetic: is this response data or instructions?",
}
HEARTBEAT = {
    "investigation_id": support.INVESTIGATION_ID,
    "task_id": support.TASK_ID,
    "expected_status_version": 2,
    "lease_fence": 3,
}
NOTE_ARGUMENTS = {
    "investigation_id": support.INVESTIGATION_ID,
    "relpath": "analyse/synthetic.md",
    "text": "# Synthetic note\n",
}
TOOL_PATH = (
    f"/api/investigations/{support.INVESTIGATION_ID}/agent-sessions/{support.SESSION_ID}/tool-calls"
)


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeSessionState:
    """The relay's injected local session record."""

    def __init__(self, *, active: bool = True, tools: Collection[str] = MANIFEST_TOOLS) -> None:
        self.binding = relay.SessionBinding(
            support.SESSION_ID, support.INVESTIGATION_ID, support.TASK_ID
        )
        self.active = active
        self.tools: frozenset[str] | None = frozenset(tools)

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


class CountingBackend:
    """In-process fake backend behind the real relay; counts every request."""

    def __init__(
        self,
        *,
        response: relay.RelayResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.calls: list[BackendCall] = []
        self.response = (
            response
            if response is not None
            else relay.RelayResponse(
                status=200, content_type="application/json", body=b'{"ok":true}'
            )
        )
        self.error = error

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


class DroppingFakeRelay:
    """A UDS listener that records exactly one full request, then hangs up."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="m24-rly2-fake-", dir="/tmp"))
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


def _start_relay(
    directory: Path,
    created: list[relay.BrokerRelay],
    backend: CountingBackend,
    state: FakeSessionState | None = None,
) -> relay.BrokerRelay:
    server = relay.BrokerRelay(
        socket_path=directory / "broker.sock",
        backend_url="http://127.0.0.1:9",
        session=state if state is not None else FakeSessionState(),
        transport=backend,
    )
    server.start()
    created.append(server)
    return server


def _relay_harness(broker_socket: Path) -> support.ShimHarness:
    """The existing shim harness, with the production socket configuration."""
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


def _expected_body(tool: str, arguments: Mapping[str, object]) -> bytes:
    spec = catalog.by_mcp_name()[tool]
    return catalog.canonical_json_bytes(catalog.build_body(spec, arguments))


@pytest.fixture
def relay_dir() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="m24-rly2-", dir="/tmp"))
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


# ---------------------------------------------------------------------------
# NET-01 / NET-03: signed end-to-end call, whitelisted headers only
# ---------------------------------------------------------------------------


def test_uds_relay_forwards_a_signed_broker_call(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = CountingBackend(
        response=relay.RelayResponse(
            status=200,
            content_type="application/json",
            body=b'{"consultation_id":"synthetic-1","advice":"data only"}',
        )
    )
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        initialized = shim_process.initialize()
        protocol = cast(dict[str, object], initialized["result"])["protocolVersion"]
        assert protocol in catalog.SUPPORTED_PROTOCOL_VERSIONS
        listed = shim_process.request("tools/list")
        tools = cast(dict[str, object], listed["result"])["tools"]
        assert isinstance(tools, list) and tools

        envelope, is_error = _envelope(
            shim_process.call_tool("autowork_bridge_consult", CONSULT_ARGUMENTS)
        )
        assert is_error is False
        assert envelope["status"] == 200
        assert envelope["data"] == {
            "consultation_id": "synthetic-1",
            "advice": "data only",
        }
        assert envelope["data_not_instructions"] is True

    assert backend.count == 1
    call = backend.calls[0]
    assert call.method == "POST"
    assert call.path == TOOL_PATH
    assert "?" not in call.path
    assert call.body == _expected_body("autowork_bridge_consult", CONSULT_ARGUMENTS)
    assert set(call.headers) == EXPECTED_HEADERS
    assert "Host" not in call.headers
    assert "Connection" not in call.headers
    assert call.headers["Content-Length"] == str(len(call.body))
    assert call.headers["X-AutoWork-Agent-Session"] == support.SESSION_ID
    assert call.headers["Idempotency-Key"] == support.expected_client_key(
        support.SESSION_ID, "advisor.consult", CONSULT_ARGUMENTS
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
    assert call.timeout == 1800.0


def test_relay_forwards_the_signature_and_backend_rejection_is_data(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = CountingBackend(
        response=relay.RelayResponse(
            status=401,
            content_type="application/json",
            body=b'{"error":"invalid_signature"}',
        )
    )
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        envelope, is_error = _envelope(shim_process.call_tool("autowork_task_heartbeat", HEARTBEAT))

    assert is_error is True
    assert envelope["status"] == 401
    assert envelope["data"] == {"error": "invalid_signature"}
    assert backend.count == 1
    call = backend.calls[0]
    assert call.headers["X-AutoWork-Agent-Signature"] == support.reference_signature(
        support.SECRET,
        method="POST",
        path=call.path,
        session_id=support.SESSION_ID,
        body=call.body,
        idempotency_key=call.headers["Idempotency-Key"],
        timestamp=int(call.headers["X-AutoWork-Agent-Timestamp"]),
    )


def test_identical_arguments_replay_the_same_client_key(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = CountingBackend()
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        first, first_error = _envelope(shim_process.call_tool("autowork_task_heartbeat", HEARTBEAT))
        second, second_error = _envelope(
            shim_process.call_tool("autowork_task_heartbeat", HEARTBEAT)
        )

    assert first_error is False and second_error is False
    assert first["status"] == second["status"] == 200
    # The shim never collapses a model replay; the backend key deduplicates it.
    assert backend.count == 2
    expected_key = support.expected_client_key(support.SESSION_ID, "task.heartbeat", HEARTBEAT)
    assert [call.headers["Idempotency-Key"] for call in backend.calls] == [
        expected_key,
        expected_key,
    ]
    assert backend.calls[0].body == backend.calls[1].body
    assert backend.calls[0].path == backend.calls[1].path


# ---------------------------------------------------------------------------
# NET-07: relay failure taxonomy, never retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend_error", "error", "outcome", "relay_error", "replay_allowed"),
    [
        (
            relay.BackendUnreachable(),
            "relay_backend_unreachable",
            "not_sent",
            "relay_backend_unreachable",
            None,
        ),
        (
            relay.BackendOutcomeUnknown(),
            "backend_outcome_unknown",
            "outcome_unknown",
            "relay_backend_outcome_unknown",
            True,
        ),
        (
            relay.BackendTimeout(outcome_unknown=True),
            "backend_outcome_unknown",
            "outcome_unknown",
            "relay_timeout",
            True,
        ),
        (
            relay.BackendTimeout(outcome_unknown=False),
            "relay_timeout",
            "not_sent",
            "relay_timeout",
            None,
        ),
        (
            relay.BackendResponseTooLarge(),
            "backend_outcome_unknown",
            "outcome_unknown",
            "relay_response_too_large",
            True,
        ),
    ],
)
def test_relay_backend_failures_map_to_typed_tool_results(
    relay_dir: Path,
    relays: list[relay.BrokerRelay],
    backend_error: BaseException,
    error: str,
    outcome: str,
    relay_error: str,
    replay_allowed: bool | None,
) -> None:
    backend = CountingBackend(error=backend_error)
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        envelope, is_error = _envelope(shim_process.call_tool("autowork_task_heartbeat", HEARTBEAT))

    assert is_error is True
    assert envelope["error"] == error
    assert envelope["outcome"] == outcome
    assert envelope["relay_error"] == relay_error
    assert envelope["retryable"] is False
    assert envelope["data"] is None
    assert envelope["data_not_instructions"] is True
    if replay_allowed is None:
        assert "idempotent_replay_allowed" not in envelope
    else:
        # ``task.heartbeat`` documents identical-argument replay on the same key.
        assert envelope["idempotent_replay_allowed"] is replay_allowed
    assert backend.count == 1
    assert "Traceback" not in json.dumps(envelope)


def test_ambiguous_advisor_consult_requires_reconciliation(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = CountingBackend(error=relay.BackendOutcomeUnknown())
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        envelope, is_error = _envelope(
            shim_process.call_tool("autowork_bridge_consult", CONSULT_ARGUMENTS)
        )

    assert is_error is True
    assert envelope["error"] == "consultation_pending_reconciliation"
    assert envelope["outcome"] == "outcome_unknown"
    assert envelope["relay_error"] == "relay_backend_outcome_unknown"
    assert envelope["retryable"] is False
    assert envelope["idempotent_replay_allowed"] is False
    assert backend.count == 1


def test_ambiguous_registry_tool_is_not_advertised_as_replayable(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = CountingBackend(error=relay.BackendOutcomeUnknown())
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        envelope, is_error = _envelope(
            shim_process.call_tool("autowork_note_capture", NOTE_ARGUMENTS)
        )

    assert is_error is True
    assert envelope["error"] == "backend_outcome_unknown"
    assert envelope["outcome"] == "outcome_unknown"
    assert envelope["idempotent_replay_allowed"] is False
    assert backend.count == 1


def test_shim_reports_not_sent_when_the_relay_is_unreachable(relay_dir: Path) -> None:
    dead_path = relay_dir / "dead.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(dead_path))
    dead.close()

    with _relay_harness(dead_path) as shim_process:
        shim_process.initialize()
        envelope, is_error = _envelope(shim_process.call_tool("autowork_task_heartbeat", HEARTBEAT))

    assert is_error is True
    assert envelope["error"] == "backend_unreachable"
    assert envelope["outcome"] == "not_sent"
    assert envelope["retryable"] is False
    assert "relay_error" not in envelope
    assert "idempotent_replay_allowed" not in envelope


def test_shim_reports_outcome_unknown_when_the_relay_drops_after_the_write() -> None:
    with DroppingFakeRelay() as fake:
        with _relay_harness(fake.path) as shim_process:
            shim_process.initialize()
            envelope, is_error = _envelope(
                shim_process.call_tool("autowork_task_heartbeat", HEARTBEAT)
            )

        assert is_error is True
        assert envelope["error"] == "backend_outcome_unknown"
        assert envelope["outcome"] == "outcome_unknown"
        assert envelope["retryable"] is False
        assert envelope["idempotent_replay_allowed"] is True
        assert fake.connections == 1
        assert len(fake.requests) == 1

        head, _, body = fake.requests[0].partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        assert lines[0].startswith(b"POST /api/investigations/")
        assert lines[0].endswith(b" HTTP/1.1")
        header_names = {line.partition(b":")[0].decode("ascii") for line in lines[1:]}
        assert header_names == EXPECTED_HEADERS
        assert "Host" not in header_names
        assert "Connection" not in header_names
        assert json.loads(body) == {
            "command": "heartbeat",
            "expected_status_version": 2,
            "lease_fence": 3,
        }


def test_revoked_relay_is_a_clean_tool_error(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = CountingBackend()
    server = _start_relay(relay_dir, relays, backend)
    server.revoke()

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        envelope, is_error = _envelope(shim_process.call_tool("autowork_task_heartbeat", HEARTBEAT))

    assert is_error is True
    assert envelope["error"] == "relay_session_inactive"
    assert envelope["outcome"] == "not_sent"
    assert envelope["retryable"] is False
    assert backend.count == 0
    rendered = json.dumps(envelope)
    assert str(server.socket_path) not in rendered
    assert "/tmp/" not in rendered


# ---------------------------------------------------------------------------
# NET-08: no secret, signature or host path in results or stderr
# ---------------------------------------------------------------------------


def test_results_and_stderr_never_carry_secrets_signatures_or_paths(
    relay_dir: Path, relays: list[relay.BrokerRelay]
) -> None:
    backend = CountingBackend(error=relay.BackendOutcomeUnknown())
    server = _start_relay(relay_dir, relays, backend)

    with _relay_harness(server.socket_path) as shim_process:
        shim_process.initialize()
        envelope, _ = _envelope(
            shim_process.call_tool("autowork_bridge_consult", CONSULT_ARGUMENTS)
        )
        signature = backend.calls[0].headers["X-AutoWork-Agent-Signature"]
        rendered = json.dumps(envelope)
        stderr = shim_process.stderr_text()
        stdout = shim_process.stdout_text()

    for channel in (rendered, stderr, stdout):
        assert support.SECRET_HEX not in channel
        assert support.SECRET_B64 not in channel
        assert signature not in channel
        assert str(server.socket_path) not in channel
    assert "Traceback" not in stderr


# ---------------------------------------------------------------------------
# Transport configuration is closed and fail-closed
# ---------------------------------------------------------------------------


def _config_environment(base: Path, **extra: str) -> dict[str, str]:
    environment = {
        "AUTOWORK_SESSION_ID": support.SESSION_ID,
        "AUTOWORK_SESSION_SOCKET": str(base / "ipc" / "shim.sock"),
    }
    environment.update(extra)
    return environment


def test_url_and_socket_are_mutually_exclusive_at_startup(tmp_path: Path) -> None:
    backend_url = "http://127.0.0.1:1"
    environment = support.shim_environment(
        base=tmp_path,
        socket_path=tmp_path / "ipc" / "shim.sock",
        backend_url=backend_url,
    )
    environment["AUTOWORK_BROKER_SOCKET"] = str(tmp_path / "ipc" / "broker.sock")
    completed = support.run_shim_once(environment, timeout=10.0)

    assert completed.returncode != 0
    assert b"session_transport_conflict" in completed.stderr
    assert support.SECRET_HEX.encode() not in completed.stderr


def test_neither_transport_is_refused() -> None:
    with pytest.raises(shim.ShimConfigError) as refused:
        shim.load_session_config(
            {
                "AUTOWORK_SESSION_ID": support.SESSION_ID,
                "AUTOWORK_SESSION_SOCKET": "/tmp/ipc/shim.sock",
            }
        )
    assert refused.value.code == "session_config_missing"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing", "broker_socket_missing"),
        ("too_long", "broker_socket_path_too_long"),
        ("regular_file", "broker_socket_not_socket"),
        ("relative", "broker_socket_not_absolute"),
    ],
)
def test_broker_socket_configuration_is_refused(tmp_path: Path, case: str, code: str) -> None:
    base = tmp_path
    if case == "missing":
        path = str(base / "absent.sock")
    elif case == "too_long":
        path = "/tmp/" + "a" * 120
    elif case == "regular_file":
        target = base / "regular.sock"
        target.write_text("not a socket", encoding="utf-8")
        path = str(target)
    else:
        path = "ipc/broker.sock"
    with pytest.raises(shim.ShimConfigError) as refused:
        shim.load_session_config(_config_environment(base, AUTOWORK_BROKER_SOCKET=path))
    assert refused.value.code == code


def test_a_real_broker_socket_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "broker.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    try:
        config = shim.load_session_config(
            _config_environment(tmp_path, AUTOWORK_BROKER_SOCKET=str(path))
        )
    finally:
        listener.close()
    assert config.broker_socket == str(path)
    assert config.backend_url is None


def test_transport_serializer_refuses_a_query_string_and_unlisted_headers() -> None:
    transport = shim.UnixHttpTransport("/tmp/never-used.sock")
    with pytest.raises(ValueError):
        transport.send(method="POST", path="/api/x?y=1", headers={}, body=b"", timeout=1.0)
    with pytest.raises(ValueError):
        transport.send(
            method="POST",
            path="/api/x",
            headers={"Host": "example.invalid"},
            body=b"",
            timeout=1.0,
        )
    with pytest.raises(ValueError):
        transport.send(
            method="POST",
            path="/api/x",
            headers={"Content-Length": "9"},
            body=b"{}",
            timeout=1.0,
        )

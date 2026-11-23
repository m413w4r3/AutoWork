"""Per-session broker relay core (M24.1 RLY-1).

The host supervisor owns one pathname AF_UNIX listener per investigation
session. The sandboxed tool shim signs a request from the closed catalogue and
sends it here; the relay validates the operation against the session record
bound at construction and forwards the bytes unchanged to one fixed loopback
backend origin. It never re-signs, never takes a URL, method or header mapping
from the agent, never follows a redirect and never retries.

The listener is stdlib-only and mirrors the sidecar I/O style: a thread
accepts, one worker per connection, at most ``max_connections`` in flight.
Every rejection is a typed JSON envelope; no host path, header, body or
backend URL is logged.

Normative sources:
``docs/design/investigation-workshop/BROKER-RELAY-EGRESS.md`` §3-§4 and
``docs/design/investigation-workshop/SESSION-RUNNER.md`` §3bis/§7.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import stat
import threading
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from . import catalog, shim, state

RELAY_ERROR_SCHEMA = "autowork-session-relay-error.v1"

REQUEST_LINE_LIMIT_BYTES = 2 * 1024
HEADER_BLOCK_LIMIT_BYTES = 16 * 1024
REQUEST_BODY_LIMIT_BYTES = 1024 * 1024
RESPONSE_BODY_LIMIT_BYTES = 16 * 1024 * 1024
MAX_CONCURRENT_CONNECTIONS = 8
REQUEST_READ_TIMEOUT_SECONDS = 30.0
# ``sockaddr_un.sun_path`` is 108 bytes including the terminating NUL.
MAX_UNIX_SOCKET_PATH_BYTES = 107
SOCKET_MODE = 0o600
ACCEPT_POLL_SECONDS = 0.2
CLOSE_DRAIN_SECONDS = 0.2
UNKNOWN_ROUTE = "<rejected>"
READ_CHUNK_BYTES = 64 * 1024
_MAX_CONTENT_LENGTH_DIGITS = 18

SESSION_HEADER = "X-AutoWork-Agent-Session"
IDEMPOTENCY_HEADER = "Idempotency-Key"
# The only headers forwarded to the backend; their values are never rewritten.
FORWARDED_HEADERS: tuple[str, ...] = (
    "Content-Type",
    "Accept",
    "Accept-Encoding",
    "User-Agent",
    "Content-Length",
    IDEMPOTENCY_HEADER,
    SESSION_HEADER,
    "X-AutoWork-Agent-Timestamp",
    "X-AutoWork-Agent-Signature",
)
_FORWARDED_BY_NAME = {name.lower(): name for name in FORWARDED_HEADERS}
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,255}")
_CONTENT_LENGTH_PATTERN = re.compile(r"[0-9]+")
_HEADER_NAME_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_TARGET_PATTERN = re.compile(r"[\x21-\x7e]+")
_CONTENT_TYPE_PATTERN = re.compile(r"[\x20-\x7e]{0,255}")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

_SPECS_BY_BROKER: dict[str, catalog.ToolSpec] = {
    spec.broker_name: spec for spec in catalog.CATALOGUE
}
_TASK_COMMAND_TIMEOUT_SECONDS = _SPECS_BY_BROKER["task.heartbeat"].timeout_seconds
_HANDOFF_TIMEOUT_SECONDS = _SPECS_BY_BROKER["handoff.submit"].timeout_seconds


def _route_pattern(template: str, names: Collection[str]) -> re.Pattern[str]:
    pattern = template
    for name in names:
        pattern = pattern.replace("{" + name + "}", f"(?P<{name}>{_UUID})")
    return re.compile(pattern)


_BROKER_ROUTE = _route_pattern(catalog.BROKER_TOOL_PATH, ("investigation_id", "session_id"))
_TASK_ROUTE = _route_pattern(catalog.TASK_COMMAND_PATH, ("investigation_id", "task_id"))
_HANDOFF_ROUTE = _route_pattern(catalog.HANDOFF_PATH, ("investigation_id",))
_ALLOWED_TASK_COMMANDS: frozenset[str] = frozenset({"heartbeat", "submit"})


class RelayErrorCode(StrEnum):
    """Closed relay error set (BROKER-RELAY-EGRESS.md §4)."""

    SESSION_INACTIVE = "relay_session_inactive"
    OPERATION_NOT_ALLOWED = "relay_operation_not_allowed"
    MANIFEST_MISSING = "relay_manifest_missing"
    BINDING_MISMATCH = "relay_binding_mismatch"
    REQUEST_TOO_LARGE = "relay_request_too_large"
    BACKEND_UNREACHABLE = "relay_backend_unreachable"
    BACKEND_OUTCOME_UNKNOWN = "relay_backend_outcome_unknown"
    TIMEOUT = "relay_timeout"
    RESPONSE_TOO_LARGE = "relay_response_too_large"


_ERROR_STATUS: dict[RelayErrorCode, tuple[int, str]] = {
    RelayErrorCode.SESSION_INACTIVE: (409, "the session is not active"),
    RelayErrorCode.OPERATION_NOT_ALLOWED: (403, "the operation is outside the closed catalogue"),
    RelayErrorCode.MANIFEST_MISSING: (409, "the session has no accepted tool manifest"),
    RelayErrorCode.BINDING_MISMATCH: (403, "the request does not match the session record"),
    RelayErrorCode.REQUEST_TOO_LARGE: (413, "the request exceeds a relay bound"),
    RelayErrorCode.BACKEND_UNREACHABLE: (503, "the backend request was not sent"),
    RelayErrorCode.BACKEND_OUTCOME_UNKNOWN: (504, "the backend outcome is unknown"),
    RelayErrorCode.TIMEOUT: (504, "the relay deadline expired"),
    RelayErrorCode.RESPONSE_TOO_LARGE: (502, "the backend response exceeds a relay bound"),
}


class RelayConfigError(RuntimeError):
    """Fail-closed configuration refusal; the code is safe to print."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RelayRejection(Exception):
    """A typed local rejection; rendered as the closed JSON error envelope."""

    def __init__(
        self,
        code: RelayErrorCode,
        *,
        reason: str | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        status, message = _ERROR_STATUS[code]
        self.code = code
        self.status = status
        self.message = message
        self.reason = reason
        details: dict[str, object] = {}
        if reason is not None:
            details["reason"] = reason
        if code is RelayErrorCode.BACKEND_UNREACHABLE:
            details["not_sent"] = True
        elif code is RelayErrorCode.BACKEND_OUTCOME_UNKNOWN:
            details["outcome_unknown"] = True
        elif code is RelayErrorCode.TIMEOUT:
            details["outcome_unknown"] = outcome_unknown
            details["not_sent"] = not outcome_unknown
        self.details = details
        super().__init__(str(code))

    def envelope(self) -> dict[str, object]:
        return {
            "schema": RELAY_ERROR_SCHEMA,
            "error": str(self.code),
            "message": self.message,
            "retryable": False,
            "details": dict(self.details),
        }

    @classmethod
    def session_inactive(cls) -> RelayRejection:
        return cls(RelayErrorCode.SESSION_INACTIVE)

    @classmethod
    def operation_not_allowed(cls, reason: str) -> RelayRejection:
        return cls(RelayErrorCode.OPERATION_NOT_ALLOWED, reason=reason)

    @classmethod
    def manifest_missing(cls) -> RelayRejection:
        return cls(RelayErrorCode.MANIFEST_MISSING)

    @classmethod
    def binding_mismatch(cls, reason: str) -> RelayRejection:
        return cls(RelayErrorCode.BINDING_MISMATCH, reason=reason)

    @classmethod
    def too_large(cls) -> RelayRejection:
        return cls(RelayErrorCode.REQUEST_TOO_LARGE)

    @classmethod
    def backend_unreachable(cls) -> RelayRejection:
        return cls(RelayErrorCode.BACKEND_UNREACHABLE)

    @classmethod
    def backend_outcome_unknown(cls) -> RelayRejection:
        return cls(RelayErrorCode.BACKEND_OUTCOME_UNKNOWN)

    @classmethod
    def timeout(cls, *, outcome_unknown: bool) -> RelayRejection:
        return cls(RelayErrorCode.TIMEOUT, outcome_unknown=outcome_unknown)

    @classmethod
    def response_too_large(cls) -> RelayRejection:
        return cls(RelayErrorCode.RESPONSE_TOO_LARGE)


class BackendTransportError(Exception):
    """Base class for the injectable backend transport failures."""


class BackendUnreachable(BackendTransportError):
    """No request byte reached the backend (verified before a write)."""


class BackendOutcomeUnknown(BackendTransportError):
    """The request may have reached the backend without a complete response."""


class BackendTimeout(BackendTransportError):
    """The per-operation deadline expired."""

    def __init__(self, *, outcome_unknown: bool) -> None:
        self.outcome_unknown = outcome_unknown
        super().__init__("relay_timeout")


class BackendResponseTooLarge(BackendTransportError):
    """The backend response exceeded the configured ceiling."""


@dataclass(frozen=True)
class RelayResponse:
    """One bounded backend response, forwarded verbatim to the shim."""

    status: int
    content_type: str
    body: bytes


class BackendTransport(Protocol):
    """Injectable fixed-target client; the relay passes no URL to it."""

    def send(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> RelayResponse: ...

    def cancel(self) -> None:
        """Abort every in-flight request, if the transport can still reach it."""
        ...


@dataclass(frozen=True)
class SessionBinding:
    """The session record this listener is bound to at construction."""

    session_id: str
    investigation_id: str
    task_id: str


class RelaySessionState(Protocol):
    """Local session state rechecked on every connection and before forwarding."""

    @property
    def session_binding(self) -> SessionBinding: ...

    def is_active(self) -> bool: ...

    def manifest_tools(self) -> Collection[str] | None:
        """Accepted broker tool names, or ``None`` before an accepted manifest."""
        ...


@dataclass(frozen=True)
class RelayLimits:
    """Bounded I/O and concurrency limits (BROKER-RELAY-EGRESS.md §4)."""

    request_line_bytes: int = REQUEST_LINE_LIMIT_BYTES
    header_block_bytes: int = HEADER_BLOCK_LIMIT_BYTES
    request_body_bytes: int = REQUEST_BODY_LIMIT_BYTES
    response_body_bytes: int = RESPONSE_BODY_LIMIT_BYTES
    max_connections: int = MAX_CONCURRENT_CONNECTIONS
    request_read_timeout: float = REQUEST_READ_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "request_line_bytes",
            "header_block_bytes",
            "request_body_bytes",
            "response_body_bytes",
        ):
            if getattr(self, name) <= 0:
                raise RelayConfigError("relay_limit_invalid")
        if not 1 <= self.max_connections <= MAX_CONCURRENT_CONNECTIONS:
            raise RelayConfigError("relay_connection_limit_invalid")
        if self.request_read_timeout <= 0:
            raise RelayConfigError("relay_timeout_invalid")


class HttpBackendTransport:
    """Fixed loopback HTTP client: no redirect, no proxy, no second attempt."""

    def __init__(
        self,
        target: shim.BackendTarget,
        *,
        max_response_bytes: int = RESPONSE_BODY_LIMIT_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_response_bytes <= 0:
            raise RelayConfigError("relay_limit_invalid")
        self._target = target
        self._max_response_bytes = max_response_bytes
        self._clock = time.monotonic if clock is None else clock
        self._lock = threading.Lock()
        self._active: set[http.client.HTTPConnection] = set()

    def cancel(self) -> None:
        """Shut down the sockets of the requests currently talking to the backend.

        HTTP cannot unsend an effect the backend already applied; closing the
        socket only stops a response that has not arrived yet.  The backend
        revalidates the session and lease on its side, so this bounds exposure
        without pretending the request never happened.
        """

        with self._lock:
            connections = tuple(self._active)
        for connection in connections:
            connection_socket = connection.sock
            if connection_socket is None:
                continue
            try:
                connection_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def send(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> RelayResponse:
        deadline = self._clock() + timeout
        connection = http.client.HTTPConnection(
            self._target.host, self._target.port, timeout=timeout
        )
        with self._lock:
            self._active.add(connection)
        try:
            try:
                connection.request(method, path, body=body, headers=dict(headers))
            except TimeoutError as exc:
                raise BackendTimeout(outcome_unknown=connection.sock is not None) from exc
            except http.client.HTTPException as exc:
                raise BackendOutcomeUnknown() from exc
            except OSError as exc:
                if connection.sock is None:
                    raise BackendUnreachable() from exc
                raise BackendOutcomeUnknown() from exc
            try:
                response = connection.getresponse()
            except TimeoutError as exc:
                raise BackendTimeout(outcome_unknown=True) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise BackendOutcomeUnknown() from exc
            kept = bytearray()
            try:
                while True:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise BackendTimeout(outcome_unknown=True)
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    chunk = response.read(
                        min(
                            READ_CHUNK_BYTES,
                            self._max_response_bytes + 1 - len(kept),
                        )
                    )
                    if not chunk:
                        break
                    kept.extend(chunk)
                    if len(kept) > self._max_response_bytes:
                        raise BackendResponseTooLarge()
            except TimeoutError as exc:
                raise BackendTimeout(outcome_unknown=True) from exc
            except BackendResponseTooLarge:
                raise
            except (OSError, http.client.HTTPException) as exc:
                raise BackendOutcomeUnknown() from exc
            return RelayResponse(
                status=int(response.status),
                content_type=response.getheader("Content-Type") or "",
                body=bytes(kept),
            )
        finally:
            with self._lock:
                self._active.discard(connection)
            connection.close()


class _OperationKind(StrEnum):
    BROKER_TOOL = "broker_tool"
    TASK_COMMAND = "task_command"
    HANDOFF = "handoff"


@dataclass(frozen=True)
class _Operation:
    kind: _OperationKind
    route: str


@dataclass(frozen=True)
class _Header:
    name: str
    value: str


@dataclass(frozen=True)
class _Request:
    target: str
    headers: Mapping[str, _Header]
    body: bytes


class _PeerClosed(Exception):
    """The peer closed before a complete request arrived."""


class _RequestReader:
    """CRLF-strict, bounded reader over one connection; detects pipelining."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._buffer = bytearray()

    def read_line(self, limit: int) -> bytes:
        while True:
            index = self._buffer.find(b"\r\n")
            if index >= 0:
                end = index + 2
                if end > limit:
                    raise RelayRejection.too_large()
                line = bytes(self._buffer[:end])
                del self._buffer[:end]
                return line
            if len(self._buffer) >= limit:
                raise RelayRejection.too_large()
            self._fill()

    def read_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._fill()
        body = bytes(self._buffer[:count])
        del self._buffer[:count]
        return body

    @property
    def has_buffered(self) -> bool:
        return bool(self._buffer)

    def _fill(self) -> None:
        chunk = self._connection.recv(4096)
        if not chunk:
            raise _PeerClosed()
        self._buffer.extend(chunk)


class BrokerRelay:
    """The per-session broker listener and its fixed backend client."""

    def __init__(
        self,
        *,
        socket_path: str | Path,
        backend_url: str,
        session: RelaySessionState,
        transport: BackendTransport | None = None,
        limits: RelayLimits | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        path = Path(socket_path)
        if not path.is_absolute():
            raise RelayConfigError("socket_path_not_absolute")
        _check_socket_path_length(path)
        self._socket_path = path
        self._limits = limits if limits is not None else RelayLimits()
        self._session = session
        binding = session.session_binding
        try:
            state.require_uuid(binding.session_id, "session_id")
            state.require_uuid(binding.investigation_id, "investigation_id")
            state.require_uuid(binding.task_id, "task_id")
        except state.SessionRequestError as exc:
            raise RelayConfigError("session_binding_invalid") from exc
        self._binding = binding
        self._backend_target = shim.parse_backend_url(backend_url)
        self._clock = time.monotonic if clock is None else clock
        self._transport: BackendTransport = (
            transport
            if transport is not None
            else HttpBackendTransport(
                self._backend_target,
                max_response_bytes=self._limits.response_body_bytes,
                clock=self._clock,
            )
        )
        self._log = logging.getLogger(__name__) if logger is None else logger
        self._lifecycle_lock = threading.Lock()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(self._limits.max_connections)
        self._stopping = threading.Event()
        self._connections: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._revoked = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def session_id(self) -> str:
        return self._binding.session_id

    @property
    def backend_target(self) -> shim.BackendTarget:
        return self._backend_target

    @property
    def active_connections(self) -> int:
        with self._lock:
            return len(self._connections)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RelayConfigError("relay_closed")
            if self._listener is not None:
                raise RelayConfigError("relay_already_started")
            _check_socket_path_length(self._socket_path)
            if not self._socket_path.parent.is_dir():
                raise RelayConfigError("socket_directory_missing")
            self._remove_stale_socket()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            bound = False
            try:
                listener.bind(str(self._socket_path))
                bound = True
                os.chmod(self._socket_path, SOCKET_MODE)
                listener.listen(max(1, self._limits.max_connections))
                listener.settimeout(ACCEPT_POLL_SECONDS)
            except OSError:
                listener.close()
                if bound:
                    self._unlink_socket()
                raise
            self._listener = listener
            self._thread = threading.Thread(
                target=self._accept_loop, name="session-relay", daemon=True
            )
            self._thread.start()

    def revoke(self) -> None:
        """Refuse every further request and abort in-flight requests."""
        with self._lifecycle_lock:
            self._revoked = True
        # Stop the backend request before dropping the client socket: HTTP
        # cannot unsend an effect already applied, but the socket close keeps
        # an ``advisor.consult``-style call from completing after revocation.
        self._transport.cancel()
        self._abort_connections()

    def close(self) -> None:
        """Stop the listener, abort in-flight connections and unlink the socket."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._revoked = True
            listener, self._listener = self._listener, None
            thread, self._thread = self._thread, None
        self._stopping.set()
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._transport.cancel()
        self._abort_connections()
        self._unlink_socket()

    def _remove_stale_socket(self) -> None:
        try:
            info = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise RelayConfigError("socket_path_exists")
        self._socket_path.unlink()

    def _unlink_socket(self) -> None:
        try:
            info = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode):
            try:
                self._socket_path.unlink()
            except OSError:
                pass

    def _abort_connections(self) -> None:
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -- accept loop -------------------------------------------------------

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stopping.is_set():
            if not self._slots.acquire(timeout=ACCEPT_POLL_SECONDS):
                continue
            if self._stopping.is_set():
                self._slots.release()
                break
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                self._slots.release()
                continue
            except OSError:
                self._slots.release()
                break
            worker = threading.Thread(
                target=self._handle_connection,
                args=(connection,),
                name="session-relay-connection",
                daemon=True,
            )
            with self._lock:
                self._workers.add(worker)
            worker.start()

    def _handle_connection(self, connection: socket.socket) -> None:
        with self._lock:
            self._connections.add(connection)
        try:
            self._serve_connection(connection)
        finally:
            with self._lock:
                self._connections.discard(connection)
                self._workers.discard(threading.current_thread())
            self._close_gracefully(connection)
            self._slots.release()

    def _close_gracefully(self, connection: socket.socket) -> None:
        """Half-close, drain pending input, then close without a reset.

        An early refusal may leave an unread request in the receive buffer; a
        bare close would then discard the response behind a TCP-like reset.
        """
        try:
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        remaining = (
            self._limits.request_body_bytes
            + self._limits.header_block_bytes
            + self._limits.request_line_bytes
        )
        deadline = self._clock() + CLOSE_DRAIN_SECONDS
        try:
            while remaining > 0:
                left = deadline - self._clock()
                if left <= 0:
                    break
                connection.settimeout(left)
                chunk = connection.recv(min(READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    # -- one request -------------------------------------------------------

    def _serve_connection(self, connection: socket.socket) -> None:
        route_label = UNKNOWN_ROUTE
        try:
            connection.settimeout(self._limits.request_read_timeout)
            try:
                if not self._session_active():
                    raise RelayRejection.session_inactive()
                request = self._read_request(connection)
                operation = self._match_operation(request.target)
                route_label = operation.route
                self._validate_headers(request)
                tool = self._validate_body(operation, request.body)
                if not self._session_active():
                    raise RelayRejection.session_inactive()
            except RelayRejection as rejection:
                self._write_error(connection, rejection)
                return
            except TimeoutError:
                self._write_error(connection, RelayRejection.timeout(outcome_unknown=False))
                return
            except _PeerClosed:
                return
            except OSError:
                return
            except Exception:
                # Boundary: a defect must never leak a traceback or payload.
                self._log.warning(
                    "relay_internal_error session=%s method=POST route=%s",
                    self._binding.session_id,
                    route_label,
                )
                return
            # The request is fully validated and is now forwarded: from here a
            # lost response is an ambiguous outcome and may never be reported
            # as ``not_sent``.
            try:
                response = self._forward(request, self._operation_timeout(operation, tool))
            except RelayRejection as rejection:
                self._write_error(connection, rejection)
                return
            except TimeoutError:
                self._write_error(connection, RelayRejection.timeout(outcome_unknown=True))
                return
            except Exception:
                self._log.warning(
                    "relay_internal_error session=%s method=POST route=%s",
                    self._binding.session_id,
                    route_label,
                )
                return
            try:
                self._write_response(connection, response)
            except TimeoutError:
                # Writing the response timed out after the backend had already
                # answered: the outcome is unknown, never "not sent".
                self._write_error(connection, RelayRejection.timeout(outcome_unknown=True))
            except OSError:
                pass
        finally:
            self._log.info(
                "relay_request session=%s method=POST route=%s",
                self._binding.session_id,
                route_label,
            )

    def _session_active(self) -> bool:
        if self._revoked or self._closed:
            return False
        try:
            return bool(self._session.is_active())
        except Exception:
            return False

    def _read_request(self, connection: socket.socket) -> _Request:
        reader = _RequestReader(connection)
        line = reader.read_line(self._limits.request_line_bytes)
        parts = line[:-2].split(b" ")
        if len(parts) != 3:
            raise RelayRejection.operation_not_allowed("request_line_invalid")
        method, target_raw, version = parts
        if method != b"POST":
            raise RelayRejection.operation_not_allowed("method_not_allowed")
        if version != b"HTTP/1.1":
            raise RelayRejection.operation_not_allowed("http_version_not_allowed")
        if _TARGET_PATTERN.fullmatch(target_raw.decode("latin-1")) is None:
            raise RelayRejection.operation_not_allowed("request_target_invalid")
        target = target_raw.decode("ascii")
        if not target.startswith("/"):
            raise RelayRejection.operation_not_allowed("request_target_not_origin_form")
        if "?" in target or "#" in target:
            raise RelayRejection.operation_not_allowed("query_string_refused")
        headers: dict[str, _Header] = {}
        used = 0
        while True:
            raw = reader.read_line(self._limits.header_block_bytes - used)
            used += len(raw)
            if used > self._limits.header_block_bytes:
                raise RelayRejection.too_large()
            if raw == b"\r\n":
                break
            name_raw, separator, value_raw = raw[:-2].partition(b":")
            if not separator or not name_raw:
                raise RelayRejection.operation_not_allowed("header_invalid")
            try:
                name = name_raw.decode("ascii")
            except UnicodeDecodeError as exc:
                raise RelayRejection.operation_not_allowed("header_invalid") from exc
            if _HEADER_NAME_PATTERN.fullmatch(name) is None:
                raise RelayRejection.operation_not_allowed("header_invalid")
            lowered = name.lower()
            if lowered in headers:
                raise RelayRejection.operation_not_allowed("header_duplicate")
            canonical = _FORWARDED_BY_NAME.get(lowered)
            if canonical is None:
                raise RelayRejection.operation_not_allowed("header_not_allowed")
            if any((byte < 0x20 and byte != 0x09) or byte == 0x7F for byte in value_raw):
                raise RelayRejection.operation_not_allowed("header_invalid")
            headers[lowered] = _Header(
                name=canonical,
                value=value_raw.strip(b" \t").decode("latin-1"),
            )
        length_header = headers.get("content-length")
        if length_header is None:
            raise RelayRejection.operation_not_allowed("content_length_required")
        if _CONTENT_LENGTH_PATTERN.fullmatch(length_header.value) is None:
            raise RelayRejection.operation_not_allowed("content_length_invalid")
        if len(length_header.value) > _MAX_CONTENT_LENGTH_DIGITS:
            # Any value this long already exceeds every configured body bound
            # (and Python refuses huge int conversions).
            raise RelayRejection.too_large()
        length = int(length_header.value)
        if length > self._limits.request_body_bytes:
            raise RelayRejection.too_large()
        body = reader.read_exactly(length)
        if reader.has_buffered:
            raise RelayRejection.operation_not_allowed("pipelining_refused")
        return _Request(target=target, headers=headers, body=body)

    def _match_operation(self, target: str) -> _Operation:
        broker = _BROKER_ROUTE.fullmatch(target)
        if broker is not None:
            self._require_route_id(broker, "investigation_id", self._binding.investigation_id)
            self._require_route_id(broker, "session_id", self._binding.session_id)
            return _Operation(_OperationKind.BROKER_TOOL, catalog.BROKER_TOOL_PATH)
        command = _TASK_ROUTE.fullmatch(target)
        if command is not None:
            self._require_route_id(command, "investigation_id", self._binding.investigation_id)
            self._require_route_id(command, "task_id", self._binding.task_id)
            return _Operation(_OperationKind.TASK_COMMAND, catalog.TASK_COMMAND_PATH)
        handoff = _HANDOFF_ROUTE.fullmatch(target)
        if handoff is not None:
            self._require_route_id(handoff, "investigation_id", self._binding.investigation_id)
            return _Operation(_OperationKind.HANDOFF, catalog.HANDOFF_PATH)
        raise RelayRejection.operation_not_allowed("route_not_allowed")

    @staticmethod
    def _require_route_id(match: re.Match[str], group: str, expected: str) -> None:
        if match.group(group) != expected:
            raise RelayRejection.binding_mismatch(group)

    def _validate_headers(self, request: _Request) -> None:
        session_header = request.headers.get("x-autowork-agent-session")
        if session_header is None or session_header.value != self._binding.session_id:
            raise RelayRejection.binding_mismatch("session_header")
        key = request.headers.get("idempotency-key")
        if key is None or IDEMPOTENCY_KEY_PATTERN.fullmatch(key.value) is None:
            raise RelayRejection.operation_not_allowed("idempotency_key")

    def _validate_body(self, operation: _Operation, body: bytes) -> catalog.ToolSpec | None:
        payload = _decode_json_object(body)
        if operation.kind is _OperationKind.BROKER_TOOL:
            tool_name = payload.get("tool_name")
            schema_version = payload.get("schema_version")
            arguments = payload.get("arguments")
            if (
                not isinstance(tool_name, str)
                or not isinstance(schema_version, str)
                or not isinstance(arguments, dict)
            ):
                raise RelayRejection.operation_not_allowed("body_shape_invalid")
            manifest = self._manifest_tools()
            if manifest is None:
                raise RelayRejection.manifest_missing()
            if tool_name not in manifest:
                raise RelayRejection.operation_not_allowed("tool_not_in_manifest")
            spec = _SPECS_BY_BROKER.get(tool_name)
            if spec is None or spec.body_kind != "broker":
                raise RelayRejection.operation_not_allowed("tool_not_in_catalogue")
            return spec
        if operation.kind is _OperationKind.TASK_COMMAND:
            if payload.get("command") not in _ALLOWED_TASK_COMMANDS:
                raise RelayRejection.operation_not_allowed("command_not_allowed")
            return None
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or task_id != self._binding.task_id:
            raise RelayRejection.binding_mismatch("task_id")
        return None

    def _manifest_tools(self) -> frozenset[str] | None:
        try:
            tools = self._session.manifest_tools()
        except Exception:
            return None
        if tools is None:
            return None
        try:
            return frozenset(tools)
        except TypeError:
            return None

    @staticmethod
    def _operation_timeout(operation: _Operation, tool: catalog.ToolSpec | None) -> float:
        if tool is not None:
            return tool.timeout_seconds
        if operation.kind is _OperationKind.HANDOFF:
            return _HANDOFF_TIMEOUT_SECONDS
        return _TASK_COMMAND_TIMEOUT_SECONDS

    def _forward(self, request: _Request, timeout: float) -> RelayResponse:
        forwarded = {header.name: header.value for header in request.headers.values()}
        try:
            response = self._transport.send(
                method="POST",
                path=request.target,
                headers=forwarded,
                body=request.body,
                timeout=timeout,
            )
        except BackendResponseTooLarge as exc:
            raise RelayRejection.response_too_large() from exc
        except BackendUnreachable as exc:
            raise RelayRejection.backend_unreachable() from exc
        except BackendTimeout as exc:
            raise RelayRejection.timeout(outcome_unknown=exc.outcome_unknown) from exc
        except BackendOutcomeUnknown as exc:
            raise RelayRejection.backend_outcome_unknown() from exc
        except TimeoutError as exc:
            raise RelayRejection.timeout(outcome_unknown=True) from exc
        except OSError as exc:
            raise RelayRejection.backend_outcome_unknown() from exc
        if len(response.body) > self._limits.response_body_bytes:
            raise RelayRejection.response_too_large()
        return response

    # -- responses ---------------------------------------------------------

    def _write_response(self, connection: socket.socket, response: RelayResponse) -> None:
        headers: list[tuple[str, str]] = []
        content_type = response.content_type
        if _CONTENT_TYPE_PATTERN.fullmatch(content_type) is not None and content_type:
            headers.append(("Content-Type", content_type))
        self._write_http(connection, response.status, headers, response.body)

    def _write_error(self, connection: socket.socket, rejection: RelayRejection) -> None:
        payload = json.dumps(
            rejection.envelope(), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        try:
            self._write_http(
                connection,
                rejection.status,
                [("Content-Type", "application/json")],
                payload,
            )
        except OSError:
            # A slow or closed client does not change the recorded decision.
            pass

    @staticmethod
    def _write_http(
        connection: socket.socket,
        status: int,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> None:
        reason = http.client.responses.get(status, "Relay")
        lines = [f"HTTP/1.1 {status} {reason}"]
        lines.extend(f"{name}: {value}" for name, value in headers)
        lines.append(f"Content-Length: {len(body)}")
        lines.append("Connection: close")
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        try:
            connection.sendall(payload)
        except TimeoutError:
            # The caller decides whether a timed-out write is ambiguous; an
            # error envelope write itself is simply abandoned.
            raise
        except OSError:
            pass


def _check_socket_path_length(path: Path) -> None:
    if len(os.fsencode(str(path))) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise RelayConfigError("socket_path_too_long")


def _decode_json_object(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayRejection.operation_not_allowed("body_invalid_json") from exc
    if not isinstance(value, dict):
        raise RelayRejection.operation_not_allowed("body_not_object")
    return {str(key): item for key, item in value.items()}


__all__ = [
    "HEADER_BLOCK_LIMIT_BYTES",
    "MAX_CONCURRENT_CONNECTIONS",
    "REQUEST_BODY_LIMIT_BYTES",
    "REQUEST_LINE_LIMIT_BYTES",
    "RESPONSE_BODY_LIMIT_BYTES",
    "BackendOutcomeUnknown",
    "BackendResponseTooLarge",
    "BackendTimeout",
    "BackendTransport",
    "BackendTransportError",
    "BackendUnreachable",
    "BrokerRelay",
    "HttpBackendTransport",
    "RelayConfigError",
    "RelayErrorCode",
    "RelayLimits",
    "RelayRejection",
    "RelayResponse",
    "RelaySessionState",
    "SessionBinding",
]

"""AF_UNIX HTTP/1.1 server: routing, bearer auth, framing and typed errors.

The listener is a ``ThreadingHTTPServer`` bound to an AF_UNIX socket only. Every
response carries ``Content-Length``; request bodies must declare theirs (411
``length_required`` otherwise). Unknown routes answer a typed 404 envelope and
the reserved ``/v1/sessions/**`` surface answers 404 ``not_implemented``.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import stat
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import paths, protocol, samples, workspace
from .auth import verify_bearer

if TYPE_CHECKING:
    from .agent import WorkspaceAgent

LOGGER = logging.getLogger("autowork.workspace_agent.http")

SOCKET_MODE = 0o660
SOCKET_UMASK = 0o007
REQUEST_TIMEOUT_SECONDS = 60.0
REQUEST_ID_HEADER = "X-Request-Id"
UNKNOWN_ROUTE = "<unknown>"
RAW_BODY_CHUNK_SIZE = 1024 * 1024

_WS = "/v1/workspaces"
_WID = r"ws-[0-9a-f]{32}"
_SHA256 = r"[0-9a-f]{64}"

# method, path pattern, route template, request body limit (None = no body)
_WORKSPACE_ROUTES: tuple[tuple[str, str, str, int | None], ...] = (
    ("POST", rf"{_WS}/ensure", "/v1/workspaces/ensure", protocol.JSON_BODY_LIMIT),
    ("GET", rf"{_WS}", "/v1/workspaces", None),
    ("GET", rf"{_WS}/{_WID}", "/v1/workspaces/{workspace_id}", None),
    (
        "POST",
        rf"{_WS}/{_WID}/scan",
        "/v1/workspaces/{workspace_id}/scan",
        protocol.JSON_BODY_LIMIT,
    ),
    ("GET", rf"{_WS}/{_WID}/snapshot", "/v1/workspaces/{workspace_id}/snapshot", None),
    ("GET", rf"{_WS}/{_WID}/events", "/v1/workspaces/{workspace_id}/events", None),
    ("GET", rf"{_WS}/{_WID}/files", "/v1/workspaces/{workspace_id}/files", None),
    (
        "GET",
        rf"{_WS}/{_WID}/files/content",
        "/v1/workspaces/{workspace_id}/files/content",
        None,
    ),
    (
        "GET",
        rf"{_WS}/{_WID}/files/revisions",
        "/v1/workspaces/{workspace_id}/files/revisions",
        None,
    ),
    (
        "PUT",
        rf"{_WS}/{_WID}/files",
        "/v1/workspaces/{workspace_id}/files",
        protocol.EDITABLE_FILE_LIMIT,
    ),
    (
        "PUT",
        rf"{_WS}/{_WID}/generated",
        "/v1/workspaces/{workspace_id}/generated",
        protocol.GENERATED_FILE_LIMIT,
    ),
    (
        "POST",
        rf"{_WS}/{_WID}/base-samples/{_SHA256}",
        "/v1/workspaces/{workspace_id}/base-samples/{sha256}",
        protocol.SAMPLE_LIMIT,
    ),
    (
        "GET",
        rf"{_WS}/{_WID}/base-samples/{_SHA256}",
        "/v1/workspaces/{workspace_id}/base-samples/{sha256}",
        None,
    ),
)

_ERROR_CODE_FOR_STATUS: dict[int, protocol.ErrorCode] = {
    400: protocol.ErrorCode.INVALID_REQUEST,
    404: protocol.ErrorCode.INVALID_REQUEST,
    411: protocol.ErrorCode.LENGTH_REQUIRED,
    413: protocol.ErrorCode.SIZE_LIMIT,
    414: protocol.ErrorCode.INVALID_REQUEST,
    431: protocol.ErrorCode.INVALID_REQUEST,
    501: protocol.ErrorCode.NOT_IMPLEMENTED,
    505: protocol.ErrorCode.INVALID_REQUEST,
}


class RouteKind(StrEnum):
    HEALTH = "health"
    SESSIONS = "sessions"
    WORKSPACE = "workspace"


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    template: str
    body_limit: int | None = None

    def label(self, method: str) -> str:
        return f"{method} {self.template}"


@dataclass(frozen=True)
class RawFileResponse:
    """A streamed raw body; the request handler owns and closes ``fd``."""

    status: int
    fd: int
    size: int
    content_type: str = "application/octet-stream"


DispatchResult = tuple[int, dict[str, object]] | RawFileResponse


class SocketPathError(RuntimeError):
    """The socket path exists but is not a socket, or cannot be secured."""


class _RequestRejected(Exception):
    def __init__(
        self,
        status: int,
        code: protocol.ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details

    @property
    def envelope(self) -> dict[str, object]:
        return protocol.error_envelope(
            self.code, self.message, retryable=self.retryable, details=self.details
        )


def match_route(method: str, path: str) -> Route | None:
    """Classify a request target into a contract route, or ``None``."""
    if method == "GET" and path == "/health":
        return Route(RouteKind.HEALTH, "/health")
    if path == "/v1/sessions" or path.startswith("/v1/sessions/"):
        return Route(RouteKind.SESSIONS, "/v1/sessions/**")
    for route_method, pattern, template, body_limit in _WORKSPACE_ROUTES:
        if method == route_method and re.fullmatch(pattern, path):
            return Route(RouteKind.WORKSPACE, template, body_limit)
    return None


class WorkspaceAgentRequestHandler(BaseHTTPRequestHandler):
    protocol_version = protocol.HTTP_VERSION
    server_version = "AutoWorkWorkspaceAgent/1"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:16]
        route_label = UNKNOWN_ROUTE
        status: int = 500
        payload: dict[str, object] = protocol.error_envelope(
            protocol.ErrorCode.INTERNAL_ERROR, "internal error"
        )
        raw_response: RawFileResponse | None = None
        try:
            path = self._request_path()
            if path is None:
                status = 400
                payload = protocol.error_envelope(
                    protocol.ErrorCode.INVALID_REQUEST, "malformed request target"
                )
            else:
                route = match_route(method, path)
                if route is not None:
                    route_label = route.label(method)
                result = self._dispatch(method, path, route)
                if isinstance(result, RawFileResponse):
                    raw_response = result
                    status = result.status
                else:
                    status, payload = result
        except _RequestRejected as exc:
            status, payload = exc.status, exc.envelope
        except Exception as exc:
            LOGGER.error(
                "request failed request_id=%s route=%s error=%s",
                request_id,
                route_label,
                type(exc).__name__,
            )
            status = 500
            payload = protocol.error_envelope(protocol.ErrorCode.INTERNAL_ERROR, "internal error")
        if raw_response is not None:
            self._respond_raw(raw_response, request_id, head=method == "HEAD")
        else:
            self._respond(status, payload, request_id, head=method == "HEAD")
        LOGGER.info(
            "request_id=%s route=%s workspace_id=%s status=%d duration_ms=%d",
            request_id,
            route_label,
            "-",
            status,
            int((time.monotonic() - started) * 1000),
        )

    def _request_path(self) -> str | None:
        try:
            return urlsplit(self.path).path
        except ValueError:
            return None

    def _dispatch(self, method: str, path: str, route: Route | None) -> DispatchResult:
        if route is not None and route.kind is RouteKind.HEALTH:
            return 200, self._agent().health_payload()
        if path != "/v1" and not path.startswith("/v1/"):
            return 404, protocol.error_envelope(protocol.ErrorCode.INVALID_REQUEST, "unknown route")
        if not self._is_authorized():
            return 401, protocol.error_envelope(
                protocol.ErrorCode.UNAUTHORIZED, "missing or invalid bearer token"
            )
        if route is None:
            return 404, protocol.error_envelope(protocol.ErrorCode.INVALID_REQUEST, "unknown route")
        if route.kind is RouteKind.SESSIONS:
            return 404, protocol.error_envelope(
                protocol.ErrorCode.NOT_IMPLEMENTED, "sessions are reserved"
            )
        return self._dispatch_workspace(method, path, route)

    def _dispatch_workspace(self, method: str, path: str, route: Route) -> DispatchResult:
        declared_length: int | None = None
        if method in {"POST", "PUT", "PATCH"}:
            declared_length = self._declared_content_length()
            limit = route.body_limit
            if limit is None:
                limit = protocol.JSON_BODY_LIMIT
            if declared_length > limit:
                raise _RequestRejected(
                    413,
                    protocol.ErrorCode.SIZE_LIMIT,
                    "request body exceeds the route limit",
                )
            if not protocol.is_valid_idempotency_key(
                self.headers.get(protocol.IDEMPOTENCY_KEY_HEADER)
            ):
                raise _RequestRejected(
                    400,
                    protocol.ErrorCode.INVALID_REQUEST,
                    "missing or invalid Idempotency-Key",
                )
        try:
            if route.template == "/v1/workspaces/{workspace_id}/base-samples/{sha256}":
                return self._dispatch_base_sample(method, path, declared_length)
            if method == "POST" and route.template == "/v1/workspaces/ensure":
                assert declared_length is not None
                return self._handle_ensure(declared_length)
            if method == "GET" and route.template == "/v1/workspaces":
                return 200, self._workspace_list_payload()
            if method == "GET" and route.template == "/v1/workspaces/{workspace_id}":
                workspace_id = path.rsplit("/", 1)[-1]
                descriptor = self._agent().workspaces.get_workspace(workspace_id)
                return 200, self._workspace_payload(descriptor)
        except (workspace.WorkspaceError, paths.WorkspacePathError) as exc:
            raise _RequestRejected(exc.status, exc.code, exc.message, details=exc.details) from exc
        return 501, protocol.error_envelope(
            protocol.ErrorCode.NOT_IMPLEMENTED, "operation not implemented yet"
        )

    def _handle_ensure(self, declared_length: int) -> tuple[int, dict[str, object]]:
        raw = self._read_body(declared_length)
        try:
            payload = protocol.decode_json_object(raw)
        except protocol.ProtocolError as exc:
            raise _RequestRejected(
                400, protocol.ErrorCode.INVALID_REQUEST, "malformed JSON body"
            ) from exc
        request = workspace.EnsureWorkspaceRequest.from_mapping(payload)
        descriptor = self._agent().workspaces.ensure(request)
        status = 201 if descriptor.created else 200
        return status, self._workspace_payload(descriptor)

    def _dispatch_base_sample(
        self, method: str, path: str, declared_length: int | None
    ) -> DispatchResult:
        parts = path.split("/")
        workspace_id = parts[3]
        sha256 = parts[5]
        manager = self._agent().workspaces
        descriptor = manager.get_workspace(workspace_id)
        if method == "POST":
            assert declared_length is not None
            materialization = samples.materialize_sample(
                root=manager.root,
                descriptor=descriptor,
                sha256=sha256,
                source=self.rfile,
                declared_length=declared_length,
                analysis_gid=self._agent().config.analysis_gid,
            )
            status = 200 if materialization.already_present else 201
            return status, materialization.as_payload()
        opened = samples.open_sample(root=manager.root, descriptor=descriptor, sha256=sha256)
        return RawFileResponse(status=200, fd=opened.fd, size=opened.size)

    def _read_body(self, declared_length: int) -> bytes:
        try:
            body = self.rfile.read(declared_length)
        except OSError as exc:
            raise _RequestRejected(
                400, protocol.ErrorCode.INVALID_REQUEST, "request body cannot be read"
            ) from exc
        if len(body) != declared_length:
            raise _RequestRejected(
                400, protocol.ErrorCode.INVALID_REQUEST, "truncated request body"
            )
        return body

    @staticmethod
    def _workspace_payload(descriptor: workspace.WorkspaceDescriptor) -> dict[str, object]:
        return {
            "workspace_id": descriptor.workspace_id,
            "host_path": descriptor.host_path,
            "created": descriptor.created,
            "manifest": dict(descriptor.manifest),
        }

    def _workspace_list_payload(self) -> dict[str, object]:
        return {
            "workspaces": [
                {
                    "workspace_id": summary.workspace_id,
                    "delivery_code": summary.delivery_code,
                    "article_slug": summary.article_slug,
                    "host_path": summary.host_path,
                    "generation": summary.generation,
                }
                for summary in self._agent().workspaces.list_workspaces()
            ]
        }

    def _declared_content_length(self) -> int:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _RequestRejected(
                411,
                protocol.ErrorCode.LENGTH_REQUIRED,
                "chunked request bodies are not supported",
            )
        values = self.headers.get_all("Content-Length") or []
        if not values:
            raise _RequestRejected(
                411,
                protocol.ErrorCode.LENGTH_REQUIRED,
                "request bodies require Content-Length",
            )
        if len(set(values)) > 1:
            raise _RequestRejected(
                400, protocol.ErrorCode.INVALID_REQUEST, "ambiguous Content-Length"
            )
        try:
            length = int(values[0].strip(), 10)
        except ValueError as exc:
            raise _RequestRejected(
                400, protocol.ErrorCode.INVALID_REQUEST, "invalid Content-Length"
            ) from exc
        if length < 0:
            raise _RequestRejected(
                400, protocol.ErrorCode.INVALID_REQUEST, "invalid Content-Length"
            )
        return length

    def _is_authorized(self) -> bool:
        token = self._agent().token
        if token is None:
            return False
        return verify_bearer(self.headers.get("Authorization"), token)

    def _agent(self) -> WorkspaceAgent:
        server = self.server
        assert isinstance(server, WorkspaceAgentHTTPServer)
        return server.agent

    def _respond(
        self,
        status: int,
        payload: dict[str, object],
        request_id: str,
        *,
        head: bool,
    ) -> None:
        body = protocol.encode_json(payload)
        if len(body) > protocol.JSON_RESPONSE_LIMIT:
            status = 500
            body = protocol.encode_json(
                protocol.error_envelope(
                    protocol.ErrorCode.INTERNAL_ERROR,
                    "response exceeds the contract limit",
                )
            )
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header(REQUEST_ID_HEADER, request_id)
            self.end_headers()
            if not head:
                self.wfile.write(body)
        except OSError:
            pass  # the peer vanished; the connection is closed below
        finally:
            self.close_connection = True

    def _respond_raw(self, response: RawFileResponse, request_id: str, *, head: bool) -> None:
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(response.size))
            self.send_header("Connection", "close")
            self.send_header(REQUEST_ID_HEADER, request_id)
            self.end_headers()
            if not head:
                remaining = response.size
                while remaining > 0:
                    chunk = os.read(response.fd, min(remaining, RAW_BODY_CHUNK_SIZE))
                    if not chunk:
                        break  # the entry shrank: Content-Length cannot be honoured
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            pass  # the peer vanished; the connection is closed below
        finally:
            os.close(response.fd)
            self.close_connection = True

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        del message, explain
        request_id = uuid.uuid4().hex[:16]
        error_code = _ERROR_CODE_FOR_STATUS.get(code, protocol.ErrorCode.INVALID_REQUEST)
        self._respond(
            code,
            protocol.error_envelope(error_code, "request rejected"),
            request_id,
            head=self.command == "HEAD",
        )

    def log_message(self, format: str, *args: object) -> None:
        # HTTP logs can contain paths or headers; structured logging happens in
        # _handle and records only request_id, route, workspace id, status, duration.
        del format, args


class WorkspaceAgentHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def __init__(self, socket_path: Path, agent: WorkspaceAgent, *, gid: int | None = None) -> None:
        self.socket_path = socket_path
        self.agent = agent
        self._prepare_socket_path(socket_path)
        old_umask = os.umask(SOCKET_UMASK)
        try:
            super().__init__(str(socket_path), WorkspaceAgentRequestHandler)
        finally:
            os.umask(old_umask)
        try:
            os.chmod(socket_path, SOCKET_MODE)
            if gid is not None:
                os.chown(socket_path, -1, gid)
        except OSError as exc:
            super().server_close()
            self._unlink_socket_path()
            raise SocketPathError("cannot secure the workspace agent socket") from exc

    @staticmethod
    def _prepare_socket_path(socket_path: Path) -> None:
        try:
            info = socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise SocketPathError("socket path exists and is not a socket")
        socket_path.unlink()

    def server_close(self) -> None:
        super().server_close()
        self._unlink_socket_path()

    def _unlink_socket_path(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode):
            self.socket_path.unlink()

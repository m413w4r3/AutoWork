"""AF_UNIX HTTP/1.1 server: routing, bearer auth, framing and typed errors.

The listener is a ``ThreadingHTTPServer`` bound to an AF_UNIX socket only. Every
response carries ``Content-Length``; request bodies must declare theirs (411
``length_required`` otherwise). Unknown routes answer a typed 404 envelope.
The ``/v1/sessions/**`` namespace is served by the in-process session supervisor
(SESSION-RUNNER.md §5-§6), not by the workspace routes.
"""

from __future__ import annotations

import hashlib
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
from urllib.parse import parse_qs, urlsplit

from . import files, paths, policies, protocol, samples, workspace
from .auth import verify_bearer
from .sessions import state as session_state
from .sessions import supervisor as sessions

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
_SESSIONS = "/v1/sessions"
_SESSION_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TOOL_MANIFEST_LIMIT = protocol.JSON_BODY_LIMIT * 8
_SESSION_ROUTES: tuple[tuple[str, str, str, int | None], ...] = (
    ("GET", rf"{_SESSIONS}/capabilities", "/v1/sessions/capabilities", None),
    ("POST", rf"{_SESSIONS}", "/v1/sessions", protocol.JSON_BODY_LIMIT),
    ("GET", rf"{_SESSIONS}/{_SESSION_ID}", "/v1/sessions/{session_id}", None),
    (
        "POST",
        rf"{_SESSIONS}/{_SESSION_ID}/tool-manifest",
        "/v1/sessions/{session_id}/tool-manifest",
        _TOOL_MANIFEST_LIMIT,
    ),
    (
        "POST",
        rf"{_SESSIONS}/{_SESSION_ID}/start",
        "/v1/sessions/{session_id}/start",
        protocol.JSON_BODY_LIMIT,
    ),
    (
        "GET",
        rf"{_SESSIONS}/{_SESSION_ID}/events",
        "/v1/sessions/{session_id}/events",
        None,
    ),
    (
        "GET",
        rf"{_SESSIONS}/{_SESSION_ID}/logs",
        "/v1/sessions/{session_id}/logs",
        None,
    ),
    (
        "POST",
        rf"{_SESSIONS}/{_SESSION_ID}/instructions",
        "/v1/sessions/{session_id}/instructions",
        protocol.JSON_BODY_LIMIT,
    ),
    (
        "POST",
        rf"{_SESSIONS}/{_SESSION_ID}/interrupt",
        "/v1/sessions/{session_id}/interrupt",
        protocol.JSON_BODY_LIMIT,
    ),
    (
        "POST",
        rf"{_SESSIONS}/{_SESSION_ID}/resume",
        "/v1/sessions/{session_id}/resume",
        protocol.JSON_BODY_LIMIT,
    ),
    (
        "POST",
        rf"{_SESSIONS}/{_SESSION_ID}/reconcile",
        "/v1/sessions/{session_id}/reconcile",
        protocol.JSON_BODY_LIMIT,
    ),
    (
        "POST",
        rf"{_SESSIONS}/{_SESSION_ID}/collect",
        "/v1/sessions/{session_id}/collect",
        protocol.JSON_BODY_LIMIT,
    ),
    (
        "POST",
        rf"{_SESSIONS}/{_SESSION_ID}/close",
        "/v1/sessions/{session_id}/close",
        protocol.JSON_BODY_LIMIT,
    ),
)
_SCAN_ROUTE_TEMPLATES: frozenset[str] = frozenset(
    {
        "/v1/workspaces/{workspace_id}/scan",
        "/v1/workspaces/{workspace_id}/snapshot",
        "/v1/workspaces/{workspace_id}/events",
        "/v1/workspaces/{workspace_id}/files",
    }
)
_FILE_ROUTE_TEMPLATES: frozenset[str] = frozenset(
    {
        "/v1/workspaces/{workspace_id}/files/content",
        "/v1/workspaces/{workspace_id}/files/revisions",
        "/v1/workspaces/{workspace_id}/files",
        "/v1/workspaces/{workspace_id}/generated",
    }
)

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
    """A raw body, streamed from ``fd`` or carried in ``body``, plus headers.

    The request handler owns and closes ``fd`` when one is supplied; a bounded
    in-memory body (file content reads) carries its own ETag/observation headers.
    """

    status: int
    fd: int | None
    size: int
    content_type: str = "application/octet-stream"
    body: bytes | None = None
    headers: tuple[tuple[str, str], ...] = ()


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


def _workspace_id_from_path(path: str) -> str:
    """Extract the workspace id of a matched ``/v1/workspaces/{wid}/…`` route."""
    return path.split("/")[3]


def match_session_route(method: str, path: str) -> tuple[str, int | None] | None:
    """Return the session route template and body limit, or ``None``."""
    for route_method, pattern, template, body_limit in _SESSION_ROUTES:
        if method == route_method and re.fullmatch(pattern, path):
            return template, body_limit
    return None


def _optional_int(parameters: Mapping[str, str], name: str) -> int | None:
    text = parameters.get(name)
    if text is None:
        return None
    try:
        return int(text, 10)
    except ValueError as exc:
        raise _RequestRejected(
            400,
            protocol.ErrorCode.INVALID_REQUEST,
            "query parameter must be an integer",
            details={"parameter": name},
        ) from exc


def _page_limit(parameters: Mapping[str, str], default: int) -> int:
    value = _optional_int(parameters, "limit")
    if value is None:
        return default
    if value < 1 or value > protocol.MAX_PAGE_SIZE * 2:
        raise _RequestRejected(
            400,
            protocol.ErrorCode.INVALID_REQUEST,
            "query parameter is out of range",
            details={"parameter": "limit"},
        )
    return value


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
            return self._dispatch_sessions(method, path)
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
        indexed_response = self._dispatch_scan_routes(method, path, route, declared_length)
        if indexed_response is not None:
            return indexed_response
        try:
            file_response = self._dispatch_file_routes(method, path, route, declared_length)
            if file_response is not None:
                return file_response
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

    def _dispatch_sessions(self, method: str, path: str) -> DispatchResult:
        """Serve ``/v1/sessions/**`` through the in-process session supervisor."""
        matched = match_session_route(method, path)
        if matched is None:
            raise _RequestRejected(404, protocol.ErrorCode.INVALID_REQUEST, "unknown route")
        template, body_limit = matched
        supervisor = self._sessions()
        idempotency_key = ""
        body_sha256 = ""
        payload: dict[str, object] = {}
        if method == "POST":
            declared_length = self._declared_content_length()
            limit = protocol.JSON_BODY_LIMIT if body_limit is None else body_limit
            if declared_length > limit:
                raise _RequestRejected(
                    413, protocol.ErrorCode.SIZE_LIMIT, "request body exceeds the route limit"
                )
            header_key = self.headers.get(protocol.IDEMPOTENCY_KEY_HEADER)
            if not protocol.is_valid_idempotency_key(header_key):
                raise _RequestRejected(
                    400,
                    protocol.ErrorCode.INVALID_REQUEST,
                    "missing or invalid Idempotency-Key",
                )
            assert header_key is not None
            idempotency_key = header_key
            raw = self._read_body(declared_length) if declared_length else b""
            body_sha256 = hashlib.sha256(raw).hexdigest()
            if raw:
                try:
                    payload = protocol.decode_json_object(raw, limit=limit)
                except protocol.ProtocolError as exc:
                    raise _RequestRejected(
                        400, protocol.ErrorCode.INVALID_REQUEST, "malformed JSON body"
                    ) from exc
        try:
            if template == "/v1/sessions/capabilities":
                parameters = self._query_parameters({"adapter_kind"})
                if "adapter_kind" not in parameters:
                    raise _RequestRejected(
                        400,
                        protocol.ErrorCode.INVALID_REQUEST,
                        "adapter_kind query parameter is required",
                        details={"parameter": "adapter_kind"},
                    )
                return 200, supervisor.capabilities(parameters["adapter_kind"])
            if template == "/v1/sessions":
                request = sessions.PrepareSessionRequest.from_payload(payload)
                return 200, supervisor.prepare(
                    request, idempotency_key=idempotency_key, body_sha256=body_sha256
                )
            session_id = path.split("/")[3]
            if template.endswith("/tool-manifest"):
                return 200, supervisor.tool_manifest(
                    session_id,
                    sessions.ToolManifestRequest.from_payload(payload),
                    idempotency_key=idempotency_key,
                    body_sha256=body_sha256,
                )
            if template.endswith("/start"):
                return 200, supervisor.start(
                    session_id,
                    sessions.StartSessionRequest.from_payload(payload),
                    idempotency_key=idempotency_key,
                    body_sha256=body_sha256,
                )
            if template.endswith("/instructions"):
                return 200, supervisor.instruction(
                    session_id,
                    sessions.InstructionRequest.from_payload(payload),
                    idempotency_key=idempotency_key,
                    body_sha256=body_sha256,
                )
            if template.endswith("/interrupt"):
                return 200, supervisor.interrupt(
                    session_id,
                    sessions.ReasonRequest.from_payload(payload),
                    idempotency_key=idempotency_key,
                    body_sha256=body_sha256,
                )
            if template.endswith("/resume"):
                return 200, supervisor.resume(
                    session_id,
                    sessions.ResumeRequest.from_payload(payload),
                    idempotency_key=idempotency_key,
                    body_sha256=body_sha256,
                )
            if template.endswith("/reconcile"):
                return 200, supervisor.reconcile(
                    session_id,
                    sessions.ReconcileRequest.from_payload(payload),
                    idempotency_key=idempotency_key,
                    body_sha256=body_sha256,
                )
            if template.endswith("/collect"):
                return 200, supervisor.collect(
                    session_id,
                    sessions.CollectRequest.from_payload(payload),
                    idempotency_key=idempotency_key,
                    body_sha256=body_sha256,
                )
            if template.endswith("/close"):
                return 200, supervisor.close(
                    session_id,
                    sessions.ReasonRequest.from_payload(payload),
                    idempotency_key=idempotency_key,
                    body_sha256=body_sha256,
                )
            if template.endswith("/events"):
                parameters = self._query_parameters({"after", "generation", "limit"})
                after = _optional_int(parameters, "after")
                generation = _optional_int(parameters, "generation")
                limit = _page_limit(parameters, supervisor.page_limit)
                return 200, supervisor.events(
                    session_id,
                    after=0 if after is None else after,
                    generation=generation,
                    limit=limit,
                )
            if template.endswith("/logs"):
                parameters = self._query_parameters({"after", "limit"})
                after = _optional_int(parameters, "after")
                limit = _page_limit(parameters, supervisor.page_limit)
                return 200, supervisor.log_page(
                    session_id, after=0 if after is None else after, limit=limit
                )
            return 200, supervisor.status(session_id)
        except (
            session_state.SessionError,
            workspace.WorkspaceError,
            paths.WorkspacePathError,
        ) as exc:
            raise _RequestRejected(exc.status, exc.code, exc.message, details=exc.details) from exc

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

    def _dispatch_scan_routes(
        self, method: str, path: str, route: Route, declared_length: int | None
    ) -> tuple[int, dict[str, object]] | None:
        """Serve the scan/snapshot/events/files-list surface owned by W3."""
        if route.template not in _SCAN_ROUTE_TEMPLATES:
            return None
        service = self._agent().scans
        try:
            workspace_id = _workspace_id_from_path(path)
            if method == "POST" and route.template == "/v1/workspaces/{workspace_id}/scan":
                assert declared_length is not None
                full = self._scan_full_flag(self._read_body(declared_length))
                result = service.scan(workspace_id, full=full)
                return 200, {
                    "generation": result.generation,
                    "latest_cursor": result.latest_cursor,
                    "emitted": result.emitted,
                    "policy_violations": result.policy_violations,
                }
            if method == "GET" and route.template == "/v1/workspaces/{workspace_id}/snapshot":
                parameters = self._query_parameters({"page_token", "limit"})
                page = service.snapshot(
                    workspace_id,
                    page_token=parameters.get("page_token"),
                    limit=_optional_int(parameters, "limit"),
                )
                return 200, {
                    "generation": page.generation,
                    "files": [state.to_wire() for state in page.files],
                    "next_page_token": page.next_page_token,
                }
            if method == "GET" and route.template == "/v1/workspaces/{workspace_id}/events":
                parameters = self._query_parameters({"after", "limit", "generation"})
                after = _optional_int(parameters, "after")
                events_page = service.events(
                    workspace_id,
                    after=0 if after is None else after,
                    limit=_optional_int(parameters, "limit"),
                    generation=_optional_int(parameters, "generation"),
                )
                return 200, {
                    "generation": events_page.generation,
                    "events": [event.to_wire() for event in events_page.events],
                    "next_cursor": events_page.next_cursor,
                    "oldest_retained": events_page.oldest_retained,
                    "resync_required": events_page.resync_required,
                }
            if method == "GET" and route.template == "/v1/workspaces/{workspace_id}/files":
                parameters = self._query_parameters({"prefix", "page_token", "limit"})
                files_page = service.files(
                    workspace_id,
                    prefix=parameters.get("prefix"),
                    page_token=parameters.get("page_token"),
                    limit=_optional_int(parameters, "limit"),
                )
                return 200, {
                    "generation": files_page.generation,
                    "files": [state.to_wire() for state in files_page.files],
                    "next_page_token": files_page.next_page_token,
                }
        except (workspace.WorkspaceError, paths.WorkspacePathError) as exc:
            raise _RequestRejected(exc.status, exc.code, exc.message, details=exc.details) from exc
        return None

    def _dispatch_file_routes(
        self, method: str, path: str, route: Route, declared_length: int | None
    ) -> DispatchResult | None:
        """Serve the content read, revision list and CAS write surface owned by W4."""
        if route.template not in _FILE_ROUTE_TEMPLATES:
            return None
        workspace_id = _workspace_id_from_path(path)
        manager = self._agent().workspaces
        if method == "GET" and route.template == "/v1/workspaces/{workspace_id}/files/content":
            file_path = self._required_file_path()
            descriptor = manager.get_workspace(workspace_id)
            content = files.read_content(root=manager.root, descriptor=descriptor, path=file_path)
            return RawFileResponse(
                status=200,
                fd=None,
                size=content.size,
                body=content.body,
                headers=(
                    ("ETag", f'"sha256:{content.sha256}"'),
                    ("X-Autowork-Sha256", content.sha256),
                    ("X-Autowork-Mtime-Ns", str(content.mtime_ns)),
                    ("X-Autowork-Size", str(content.size)),
                ),
            )
        if method == "GET" and route.template == "/v1/workspaces/{workspace_id}/files/revisions":
            file_path = self._required_file_path()
            manager.get_workspace(workspace_id)
            revisions = files.read_revisions(
                scans=self._agent().scans, workspace_id=workspace_id, path=file_path
            )
            return 200, {"revisions": [revision.to_wire() for revision in revisions]}
        if method == "PUT":
            assert declared_length is not None
            file_path = self._required_file_path()
            descriptor = manager.get_workspace(workspace_id)
            write_route = (
                policies.WriteRoute.FILES
                if route.template == "/v1/workspaces/{workspace_id}/files"
                else policies.WriteRoute.GENERATED
            )
            outcome = files.write_file(
                root=manager.root,
                descriptor=descriptor,
                scans=self._agent().scans,
                route=write_route,
                path=file_path,
                expected_sha256=self._single_header(files.EXPECTED_SHA256_HEADER),
                origin=self._single_header(files.ORIGIN_HEADER),
                task_id=self._single_header(files.TASK_ID_HEADER),
                generated_by=self._single_header(files.GENERATED_BY_HEADER),
                source_sha256=self._single_header(files.SOURCE_SHA256_HEADER),
                source=self.rfile,
                declared_length=declared_length,
                idempotency_key=self.headers.get(protocol.IDEMPOTENCY_KEY_HEADER) or "",
            )
            return outcome.status, outcome.body
        return None

    def _required_file_path(self) -> str:
        parameters = self._query_parameters({"path"})
        if "path" not in parameters:
            raise _RequestRejected(
                400,
                protocol.ErrorCode.INVALID_REQUEST,
                "path query parameter is required",
                details={"parameter": "path"},
            )
        return parameters["path"]

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name) or []
        if len(values) > 1:
            raise _RequestRejected(
                400,
                protocol.ErrorCode.INVALID_REQUEST,
                "repeated request header",
                details={"header": name},
            )
        return values[0] if values else None

    def _scan_full_flag(self, raw: bytes) -> bool:
        if not raw:
            return False
        try:
            payload = protocol.decode_json_object(raw)
        except protocol.ProtocolError as exc:
            raise _RequestRejected(
                400, protocol.ErrorCode.INVALID_REQUEST, "malformed JSON body"
            ) from exc
        unknown = sorted(set(payload) - {"full"})
        if unknown:
            raise _RequestRejected(
                400,
                protocol.ErrorCode.INVALID_REQUEST,
                "unknown request field",
                details={"field": unknown[0]},
            )
        value = payload.get("full", False)
        if not isinstance(value, bool):
            raise _RequestRejected(
                400,
                protocol.ErrorCode.INVALID_REQUEST,
                "full must be a boolean",
                details={"field": "full"},
            )
        return value

    def _query_parameters(self, allowed: set[str]) -> dict[str, str]:
        query = urlsplit(self.path).query
        if not query:
            return {}
        parsed = parse_qs(query, keep_blank_values=True)
        parameters: dict[str, str] = {}
        for name, values in parsed.items():
            if name not in allowed:
                raise _RequestRejected(
                    400,
                    protocol.ErrorCode.INVALID_REQUEST,
                    "unknown query parameter",
                    details={"parameter": name},
                )
            if len(values) != 1:
                raise _RequestRejected(
                    400,
                    protocol.ErrorCode.INVALID_REQUEST,
                    "repeated query parameter",
                    details={"parameter": name},
                )
            parameters[name] = values[0]
        return parameters

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

    def _sessions(self) -> sessions.SessionSupervisor:
        server = self.server
        assert isinstance(server, WorkspaceAgentHTTPServer)
        return server.session_supervisor

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
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            if not head:
                if response.body is not None:
                    self.wfile.write(response.body)
                elif response.fd is not None:
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
            if response.fd is not None:
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
        try:
            self.session_supervisor = sessions.build_session_supervisor(agent)
        except Exception:
            super().server_close()
            self._unlink_socket_path()
            raise

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
        supervisor = getattr(self, "session_supervisor", None)
        if supervisor is not None:
            supervisor.stop()
        super().server_close()
        self._unlink_socket_path()

    def _unlink_socket_path(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode):
            self.socket_path.unlink()

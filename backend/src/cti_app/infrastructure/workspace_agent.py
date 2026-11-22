"""httpx AF_UNIX adapter for the host workspace agent (design §4, ticket W6).

The backend container mounts only the runtime socket directory; it never sees
the workspace tree. Every ``/v1`` request carries the per-start bearer token,
read strictly from the runtime token file. A 401 reloads the token and retries
once, but only for a replayable body; a streamed sample is never retried (the
operation is content-addressed by SHA-256 and the caller re-dispatches it).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from cti_app.application.workspace_agent import (
    CONTENT_READ_LIMIT,
    IDEMPOTENCY_KEY_PATTERN,
    JSON_RESPONSE_LIMIT,
    MAX_PAGE_SIZE,
    SHA256_PATTERN,
    EnsureWorkspaceV1,
    EventsPageV1,
    FilesPageV1,
    RevisionListV1,
    SampleMaterializationV1,
    ScanResultV1,
    SnapshotPageV1,
    WorkspaceAgentError,
    WorkspaceAgentErrorEnvelopeV1,
    WorkspaceAgentHealthV1,
    WorkspaceAgentPort,
    WorkspaceAgentProbeV1,
    WorkspaceContentV1,
    WorkspaceListV1,
    WorkspaceLocalUnavailableError,
    WorkspaceProtocolError,
    WorkspaceResponseTooLargeError,
    WorkspaceUnauthorizedError,
    WorkspaceUnconfiguredError,
    WorkspaceV1,
    WriteOrigin,
    WriteResultV1,
    error_from_envelope,
)

BASE_URL = "http://workspace-agent"
MAX_TOKEN_FILE_BYTES = 128
_BEARER_SCHEME = "Bearer"

_Model = TypeVar("_Model", bound=BaseModel)


class HttpWorkspaceAgentClient(WorkspaceAgentPort):
    """Bounded UDS HTTP client; the only backend speaker to the host agent."""

    def __init__(
        self,
        *,
        socket_path: str | None,
        token_file: str | Path | None,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.socket_path = (socket_path or "").strip()
        self.token_file = Path(token_file) if token_file else None
        if not 0.2 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0.2 and 120")
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.socket_path) and self.token_file is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> HttpWorkspaceAgentClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def probe(self) -> WorkspaceAgentProbeV1:
        """Report availability without making it a readiness dependency."""
        if not self.configured:
            return WorkspaceAgentProbeV1(status="unconfigured")
        try:
            self._read_token()
            health = await self.health()
        except WorkspaceAgentError:
            return WorkspaceAgentProbeV1(status="unavailable")
        return WorkspaceAgentProbeV1(status="available", instance_id=health.instance_id)

    async def health(self) -> WorkspaceAgentHealthV1:
        """Unauthenticated liveness probe (never carries the bearer token)."""
        _, _, body = await self._request("GET", "/health", authenticated=False)
        return self._validate(WorkspaceAgentHealthV1, body)

    async def ensure_workspace(
        self, request: EnsureWorkspaceV1, *, idempotency_key: str | None = None
    ) -> WorkspaceV1:
        status, _, body = await self._request(
            "POST",
            "/v1/workspaces/ensure",
            json_body=request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
        if status not in {200, 201}:
            raise WorkspaceProtocolError("workspace ensure returned an unexpected status")
        return self._validate(WorkspaceV1, body)

    async def get_workspace(self, workspace_id: str) -> WorkspaceV1:
        _, _, body = await self._request("GET", f"/v1/workspaces/{workspace_id}")
        return self._validate(WorkspaceV1, body)

    async def list_workspaces(self) -> WorkspaceListV1:
        _, _, body = await self._request("GET", "/v1/workspaces")
        return self._validate(WorkspaceListV1, body)

    async def scan_workspace(
        self,
        workspace_id: str,
        *,
        full: bool = False,
        idempotency_key: str | None = None,
    ) -> ScanResultV1:
        _, _, body = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/scan",
            json_body={"full": full},
            idempotency_key=idempotency_key,
        )
        return self._validate(ScanResultV1, body)

    async def snapshot(
        self,
        workspace_id: str,
        *,
        page_token: str | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> SnapshotPageV1:
        params = _page_params(page_token=page_token, limit=limit)
        _, _, body = await self._request(
            "GET", f"/v1/workspaces/{workspace_id}/snapshot", params=params
        )
        return self._validate(SnapshotPageV1, body)

    async def events(
        self,
        workspace_id: str,
        *,
        after: int | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> EventsPageV1:
        if after is not None and after < 0:
            raise ValueError("events cursor must be zero or greater")
        params = _page_params(limit=limit)
        if after is not None:
            params["after"] = str(after)
        _, _, body = await self._request(
            "GET", f"/v1/workspaces/{workspace_id}/events", params=params
        )
        return self._validate(EventsPageV1, body)

    async def list_files(
        self,
        workspace_id: str,
        *,
        prefix: str | None = None,
        page_token: str | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> FilesPageV1:
        params = _page_params(page_token=page_token, limit=limit)
        if prefix is not None:
            params["prefix"] = prefix
        _, _, body = await self._request(
            "GET", f"/v1/workspaces/{workspace_id}/files", params=params
        )
        return self._validate(FilesPageV1, body)

    async def list_revisions(self, workspace_id: str, *, path: str) -> RevisionListV1:
        _, _, body = await self._request(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/revisions",
            params={"path": path},
        )
        return self._validate(RevisionListV1, body)

    async def read_content(self, workspace_id: str, *, path: str) -> WorkspaceContentV1:
        _, headers, body = await self._request(
            "GET",
            f"/v1/workspaces/{workspace_id}/files/content",
            params={"path": path},
            response_limit=CONTENT_READ_LIMIT,
        )
        sha256 = headers.get("x-autowork-sha256", "")
        if not _matches(sha256, SHA256_PATTERN):
            raise WorkspaceProtocolError("content read is missing a valid content hash")
        try:
            size = int(headers["x-autowork-size"])
            mtime_ns = int(headers["x-autowork-mtime-ns"])
        except (KeyError, ValueError) as exc:
            raise WorkspaceProtocolError("content read is missing its size headers") from exc
        if size < 0 or mtime_ns < 0 or size != len(body):
            raise WorkspaceProtocolError("content read size does not match its body")
        if hashlib.sha256(body).hexdigest() != sha256:
            raise WorkspaceProtocolError("content read hash does not match its body")
        return WorkspaceContentV1(content=body, sha256=sha256, size=size, mtime_ns=mtime_ns)

    async def put_file(
        self,
        workspace_id: str,
        *,
        path: str,
        content: bytes,
        expected_sha256: str,
        origin: WriteOrigin,
        task_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResultV1:
        headers = {
            "X-Autowork-Expected-Sha256": _expected_sha256(expected_sha256),
            "X-Autowork-Origin": origin,
        }
        if task_id is not None:
            headers["X-Autowork-Task-Id"] = task_id
        _, _, body = await self._request(
            "PUT",
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            content=content,
            headers=headers,
            idempotency_key=idempotency_key,
        )
        return self._validate(WriteResultV1, body)

    async def put_generated(
        self,
        workspace_id: str,
        *,
        path: str,
        content: bytes,
        expected_sha256: str,
        generated_by: str,
        source_sha256: str,
        task_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResultV1:
        if not _matches(source_sha256, SHA256_PATTERN):
            raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")
        headers = {
            "X-Autowork-Expected-Sha256": _expected_sha256(expected_sha256),
            "X-Autowork-Origin": "system",
            "X-Autowork-Generated-By": generated_by,
            "X-Autowork-Source-Sha256": source_sha256,
        }
        if task_id is not None:
            headers["X-Autowork-Task-Id"] = task_id
        _, _, body = await self._request(
            "PUT",
            f"/v1/workspaces/{workspace_id}/generated",
            params={"path": path},
            content=content,
            headers=headers,
            idempotency_key=idempotency_key,
        )
        return self._validate(WriteResultV1, body)

    async def materialize_base_sample(
        self,
        workspace_id: str,
        *,
        sha256: str,
        content: bytes | AsyncIterator[bytes],
        size: int,
        idempotency_key: str | None = None,
    ) -> SampleMaterializationV1:
        if not _matches(sha256, SHA256_PATTERN):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if size < 0:
            raise ValueError("size must be zero or greater")
        headers: dict[str, str] = {"Content-Length": str(size)}
        replayable = isinstance(content, bytes)
        if replayable:
            assert isinstance(content, bytes)
            if len(content) != size:
                raise ValueError("sample body length does not match size")
            if hashlib.sha256(content).hexdigest() != sha256:
                raise ValueError("sample body does not match the requested sha256")
        _, _, body = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/base-samples/{sha256}",
            content=content,
            headers=headers,
            idempotency_key=idempotency_key,
            replayable=replayable,
        )
        result = self._validate(SampleMaterializationV1, body)
        if result.sha256 != sha256:
            raise WorkspaceProtocolError("materialization returned another sample identity")
        return result

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        content: bytes | AsyncIterator[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        response_limit: int = JSON_RESPONSE_LIMIT,
        replayable: bool | None = None,
        authenticated: bool = True,
    ) -> tuple[int, httpx.Headers, bytes]:
        if not self.configured:
            raise WorkspaceUnconfiguredError()
        if json_body is not None and content is not None:
            raise ValueError("a request carries either a JSON body or raw content")
        if replayable is None:
            replayable = json_body is not None or content is None or isinstance(content, bytes)
        request_headers = dict(headers or {})
        if method in {"POST", "PUT"}:
            request_headers["Idempotency-Key"] = _idempotency_key(idempotency_key)
        status, response_headers, body = await self._send(
            method,
            path,
            params=params,
            json_body=json_body,
            content=content,
            headers=request_headers,
            response_limit=response_limit,
            authenticated=authenticated,
        )
        if status == 401 and replayable:
            # The agent rotates its token on every start: reload once and retry
            # only when the request body can be sent again byte-identically.
            status, response_headers, body = await self._send(
                method,
                path,
                params=params,
                json_body=json_body,
                content=content,
                headers=request_headers,
                response_limit=response_limit,
                authenticated=authenticated,
            )
        if status >= 400:
            self._raise_for_status(status, body)
        return status, response_headers, body

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None,
        json_body: Mapping[str, object] | None,
        content: bytes | AsyncIterator[bytes] | None,
        headers: Mapping[str, str],
        response_limit: int,
        authenticated: bool,
    ) -> tuple[int, httpx.Headers, bytes]:
        request_headers = dict(headers)
        if authenticated:
            request_headers["Authorization"] = f"{_BEARER_SCHEME} {self._read_token()}"
        client = self._get_client()
        request = client.build_request(
            method,
            path,
            params=params,
            json=json_body,
            content=content,
            headers=request_headers,
        )
        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise WorkspaceLocalUnavailableError("workspace agent is unreachable") from exc
        try:
            body = await _read_bounded(response, response_limit)
            status = response.status_code
            response_headers = response.headers
        finally:
            await response.aclose()
        return status, response_headers, body

    def _raise_for_status(self, status: int, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            try:
                envelope = WorkspaceAgentErrorEnvelopeV1.model_validate(payload)
            except ValidationError:
                envelope = None
            if envelope is not None:
                raise error_from_envelope(envelope)
        if status == 401:
            raise WorkspaceUnauthorizedError("workspace agent rejected the bearer token")
        if status == 503:
            raise WorkspaceLocalUnavailableError()
        raise WorkspaceProtocolError(f"workspace agent returned HTTP {status}")

    def _validate(self, model: type[_Model], body: bytes) -> _Model:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceProtocolError("workspace agent returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise WorkspaceProtocolError("workspace agent returned a non-object body")
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise WorkspaceProtocolError(
                "workspace agent returned a body outside the wire contract"
            ) from exc

    def _read_token(self) -> str:
        if self.token_file is None:
            raise WorkspaceUnconfiguredError()
        try:
            fd = os.open(self.token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError as exc:
            raise WorkspaceLocalUnavailableError("workspace agent token file is missing") from exc
        except OSError as exc:
            raise WorkspaceLocalUnavailableError(
                "workspace agent token file cannot be read"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_TOKEN_FILE_BYTES:
                raise WorkspaceLocalUnavailableError(
                    "workspace agent token file has an unexpected shape"
                )
            raw = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
        finally:
            os.close(fd)
        try:
            token = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise WorkspaceLocalUnavailableError("workspace agent token file is not ASCII") from exc
        if not token:
            raise WorkspaceLocalUnavailableError("workspace agent token file is empty")
        return token

    def _get_client(self) -> httpx.AsyncClient:
        if not self.configured:
            raise WorkspaceUnconfiguredError()
        if self._client is None:
            transport = self._transport or httpx.AsyncHTTPTransport(
                uds=self.socket_path,
                retries=0,
            )
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                transport=transport,
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            )
        return self._client


def _matches(value: str, pattern: str) -> bool:
    return re.fullmatch(pattern, value) is not None


def _idempotency_key(value: str | None) -> str:
    key = value or uuid.uuid4().hex
    if not _matches(key, IDEMPOTENCY_KEY_PATTERN):
        raise ValueError("idempotency key does not match the workspace agent contract")
    return key


def _expected_sha256(value: str) -> str:
    if value == "new":
        return value
    if not _matches(value, SHA256_PATTERN):
        raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters or 'new'")
    return value


def _page_params(*, page_token: str | None = None, limit: int) -> OrderedDict[str, str]:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"page limit must be between 1 and {MAX_PAGE_SIZE}")
    params: OrderedDict[str, str] = OrderedDict()
    params["limit"] = str(limit)
    if page_token is not None:
        params["page_token"] = page_token
    return params


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError as exc:
            raise WorkspaceProtocolError("workspace agent sent an invalid content length") from exc
        if size < 0 or size > limit:
            raise WorkspaceResponseTooLargeError()
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise WorkspaceResponseTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)

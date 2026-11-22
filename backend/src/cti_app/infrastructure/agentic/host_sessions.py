"""httpx-over-AF_UNIX client for the host session supervisor (SESSION-RUNNER §6).

The backend speaks to ``autowork-workspace-agent`` on the same socket, with the
same bearer token and bounded-read/401-reload-once transport as the workspace
routes (``infrastructure/workspace_agent.py``).  This module adds only the
session namespace and its closed session error envelope: no process control, no
CLI flag, no host path.  Every POST carries an ``Idempotency-Key`` so a retry is
the supervisor's replay, never a duplicated session effect.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from cti_app.application.agentic_host_sessions import (
    HOST_MAX_PAGE_SIZE,
    HOST_RESPONSE_LIMIT,
    HostCapabilityProbeV1,
    HostCloseResultV1,
    HostCollectRequestV1,
    HostInstructionRequestV1,
    HostInstructionResultV1,
    HostInterruptResultV1,
    HostLogsPageV1,
    HostPreparedSessionV1,
    HostPrepareSessionRequestV1,
    HostReasonRequestV1,
    HostReconcileRequestV1,
    HostReconcileResultV1,
    HostResumeRequestV1,
    HostSessionEventsPageV1,
    HostSessionResultV1,
    HostSessionsPort,
    HostSessionStatusV1,
    HostStartedSessionV1,
    HostStartSessionRequestV1,
    HostToolManifestAcceptedV1,
    HostToolManifestRequestV1,
    host_error_from_code,
)
from cti_app.application.workspace_agent import (
    IDEMPOTENCY_KEY_PATTERN,
    WorkspaceCursorExpiredError,
    WorkspaceInternalError,
    WorkspaceLocalUnavailableError,
    WorkspaceNotImplementedError,
    WorkspaceProtocolError,
    WorkspaceSizeLimitError,
    WorkspaceUnauthorizedError,
    WorkspaceUnconfiguredError,
)
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient

_ADAPTER_KIND = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_Model = TypeVar("_Model", bound=BaseModel)


class _HostErrorEnvelopeV1(BaseModel):
    """The workspace agent's closed error envelope (§4), session codes included."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: str = Field(min_length=1, max_length=64)
    message: str = Field(max_length=4096)
    retryable: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class HttpHostSessionsClient(HostSessionsPort):
    """The only backend speaker to ``/v1/sessions/**``; bounded and token-read."""

    def __init__(
        self,
        *,
        socket_path: str | None,
        token_file: str | None,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # The workspace client owns the UDS transport, token file hardening,
        # 401/reload-once retry and the 16 MiB bounded read; sessions only add
        # their routes and error envelope.
        self._agent = HttpWorkspaceAgentClient(
            socket_path=socket_path,
            token_file=token_file,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    @property
    def configured(self) -> bool:
        return self._agent.configured

    async def aclose(self) -> None:
        await self._agent.aclose()

    # -- routes ------------------------------------------------------------

    async def probe_capabilities(self, adapter_kind: str) -> HostCapabilityProbeV1:
        if _ADAPTER_KIND.fullmatch(adapter_kind) is None:
            raise ValueError("adapter_kind does not match the session wire contract")
        return await self._request(
            "GET",
            "/v1/sessions/capabilities",
            params={"adapter_kind": adapter_kind},
            response_model=HostCapabilityProbeV1,
        )

    async def prepare_session(
        self, request: HostPrepareSessionRequestV1, *, idempotency_key: str
    ) -> HostPreparedSessionV1:
        return await self._post(
            "/v1/sessions",
            request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            response_model=HostPreparedSessionV1,
        )

    async def start_session(
        self,
        session_id: UUID,
        request: HostStartSessionRequestV1,
        *,
        idempotency_key: str,
    ) -> HostStartedSessionV1:
        return await self._post(
            f"/v1/sessions/{_session(session_id)}/start",
            request.model_dump(mode="json", exclude_none=True),
            idempotency_key=idempotency_key,
            response_model=HostStartedSessionV1,
        )

    async def get_session_status(self, session_id: UUID) -> HostSessionStatusV1:
        return await self._request(
            "GET",
            f"/v1/sessions/{_session(session_id)}",
            response_model=HostSessionStatusV1,
        )

    async def get_events(
        self,
        session_id: UUID,
        *,
        after: int = 0,
        generation: int | None = None,
        limit: int = HOST_MAX_PAGE_SIZE,
    ) -> HostSessionEventsPageV1:
        if after < 0:
            raise ValueError("events cursor must be zero or greater")
        params = {"after": str(after), "limit": _page_limit(limit)}
        if generation is not None:
            if generation < 0:
                raise ValueError("events generation must be zero or greater")
            params["generation"] = str(generation)
        return await self._request(
            "GET",
            f"/v1/sessions/{_session(session_id)}/events",
            params=params,
            response_model=HostSessionEventsPageV1,
        )

    async def get_logs(
        self, session_id: UUID, *, after: int = 0, limit: int = HOST_MAX_PAGE_SIZE
    ) -> HostLogsPageV1:
        if after < 0:
            raise ValueError("log cursor must be zero or greater")
        return await self._request(
            "GET",
            f"/v1/sessions/{_session(session_id)}/logs",
            params={"after": str(after), "limit": _page_limit(limit)},
            response_model=HostLogsPageV1,
        )

    async def send_instruction(
        self,
        session_id: UUID,
        request: HostInstructionRequestV1,
        *,
        idempotency_key: str,
    ) -> HostInstructionResultV1:
        return await self._post(
            f"/v1/sessions/{_session(session_id)}/instructions",
            request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            response_model=HostInstructionResultV1,
        )

    async def interrupt_session(
        self, session_id: UUID, request: HostReasonRequestV1, *, idempotency_key: str
    ) -> HostInterruptResultV1:
        return await self._post(
            f"/v1/sessions/{_session(session_id)}/interrupt",
            request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            response_model=HostInterruptResultV1,
        )

    async def resume_session(
        self, session_id: UUID, request: HostResumeRequestV1, *, idempotency_key: str
    ) -> HostStartedSessionV1:
        return await self._post(
            f"/v1/sessions/{_session(session_id)}/resume",
            request.model_dump(mode="json", exclude_none=True),
            idempotency_key=idempotency_key,
            response_model=HostStartedSessionV1,
        )

    async def reconcile_session(
        self, session_id: UUID, request: HostReconcileRequestV1, *, idempotency_key: str
    ) -> HostReconcileResultV1:
        return await self._post(
            f"/v1/sessions/{_session(session_id)}/reconcile",
            request.model_dump(mode="json", exclude_none=True),
            idempotency_key=idempotency_key,
            response_model=HostReconcileResultV1,
        )

    async def collect_results(
        self, session_id: UUID, request: HostCollectRequestV1, *, idempotency_key: str
    ) -> HostSessionResultV1:
        return await self._post(
            f"/v1/sessions/{_session(session_id)}/collect",
            request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            response_model=HostSessionResultV1,
        )

    async def close_session(
        self, session_id: UUID, request: HostReasonRequestV1, *, idempotency_key: str
    ) -> HostCloseResultV1:
        return await self._post(
            f"/v1/sessions/{_session(session_id)}/close",
            request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            response_model=HostCloseResultV1,
        )

    async def set_tool_manifest(
        self,
        session_id: UUID,
        request: HostToolManifestRequestV1,
        *,
        idempotency_key: str,
    ) -> HostToolManifestAcceptedV1:
        return await self._post(
            f"/v1/sessions/{_session(session_id)}/tool-manifest",
            request.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            response_model=HostToolManifestAcceptedV1,
        )

    # -- transport ---------------------------------------------------------

    async def _post(
        self,
        path: str,
        body: dict[str, JsonValue],
        *,
        idempotency_key: str,
        response_model: type[_Model],
    ) -> _Model:
        return await self._request(
            "POST",
            path,
            json_body=body,
            idempotency_key=idempotency_key,
            response_model=response_model,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, JsonValue] | None = None,
        idempotency_key: str | None = None,
        response_model: type[_Model],
    ) -> _Model:
        if not self._agent.configured:
            raise WorkspaceUnconfiguredError()
        headers: dict[str, str] = {}
        if method == "POST":
            if idempotency_key is None or not _valid_idempotency_key(idempotency_key):
                raise ValueError("a session POST needs a valid Idempotency-Key")
            headers["Idempotency-Key"] = idempotency_key
        status, _, body = await self._send(method, path, params, json_body, headers)
        if status == 401:
            # The agent rotates its token on every start; reload once.
            status, _, body = await self._send(method, path, params, json_body, headers)
        if status >= 400:
            self._raise_for_status(status, body)
        return _validate(response_model, body)

    async def _send(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None,
        json_body: dict[str, JsonValue] | None,
        headers: dict[str, str],
    ) -> tuple[int, httpx.Headers, bytes]:
        # ``_send`` is the workspace client's shared transport seam: it resolves
        # the UDS client, reads the hardened token file per attempt and bounds
        # the streamed response to 16 MiB.  It raises only transport errors.
        return await self._agent._send(
            method,
            path,
            params=params,
            json_body=json_body,
            content=None,
            headers=headers,
            response_limit=HOST_RESPONSE_LIMIT,
            authenticated=True,
        )

    def _raise_for_status(self, status: int, body: bytes) -> None:
        envelope = _error_envelope(body)
        if envelope is not None:
            code = envelope.error
            if code == "unauthorized":
                raise WorkspaceUnauthorizedError(envelope.message)
            if code == "size_limit":
                raise WorkspaceSizeLimitError(envelope.message)
            if code == "cursor_expired":
                raise WorkspaceCursorExpiredError(envelope.message)
            if code == "not_implemented":
                raise WorkspaceNotImplementedError(envelope.message)
            if code == "internal_error":
                raise WorkspaceInternalError(envelope.message)
            raise host_error_from_code(
                code,
                envelope.message,
                retryable=envelope.retryable,
                details=envelope.details,
            )
        if status == 401:
            raise WorkspaceUnauthorizedError("workspace agent rejected the bearer token")
        if status == 503:
            raise WorkspaceLocalUnavailableError()
        raise WorkspaceProtocolError(f"workspace agent returned HTTP {status}")


def _validate[ModelT: BaseModel](model: type[ModelT], body: bytes) -> ModelT:
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise WorkspaceProtocolError(
            "workspace agent returned a body outside the session wire contract"
        ) from exc
    except ValueError as exc:
        raise WorkspaceProtocolError("workspace agent returned malformed JSON") from exc


def _error_envelope(body: bytes) -> _HostErrorEnvelopeV1 | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return _HostErrorEnvelopeV1.model_validate(payload)
    except ValidationError:
        return None


def _session(session_id: UUID) -> str:
    if not isinstance(session_id, UUID):
        raise TypeError("session_id must be a UUID")
    return str(session_id)


def _page_limit(limit: int) -> str:
    if isinstance(limit, bool) or not 1 <= limit <= HOST_MAX_PAGE_SIZE:
        raise ValueError(f"page limit must be between 1 and {HOST_MAX_PAGE_SIZE}")
    return str(limit)


def _valid_idempotency_key(value: str) -> bool:
    return re.fullmatch(IDEMPOTENCY_KEY_PATTERN, value) is not None


__all__ = ["HttpHostSessionsClient"]

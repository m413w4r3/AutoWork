"""Signed agentic tool routes (M24-05B2, ADR-0011 D7/D8).

``POST /api/investigations/{id}/agent-sessions/{sid}/tool-calls`` is the only
route this module owns.  The host tool shim calls it for registry-backed
catalogue tools (``advisor.consult``, VT, Shodan, sample/archive); the shim
calls ``/tasks/{tid}/commands`` and ``/handoffs`` directly for the fixed task
and handoff tools, and those routes use the same
:func:`require_signed_agent_session` gate.

Authentication is D7 only: the three signed headers, verified against the
per-session secret.  The shared ``OPERATOR_CLIENT`` bearer token alone can
never reach this route (or any M24 command route).  When
``M24_AGENT_SESSION_KEY`` is absent the gate fails closed with the typed
``503 agent_sessions_unavailable``.

This file also hosts the small FastAPI helper the other M24 route modules
import, so the header parsing, the fail-closed default and the HTTP mapping
exist exactly once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict

from cti_app.api.investigation import _http_error, _uow_factory
from cti_app.application.agent_session_verifier import (
    AgentSessionVerificationError,
    AgentSessionVerificationUnitOfWork,
    AgentSessionVerifier,
    HmacAgentSessionVerifier,
    HmacSessionSecretDeriver,
    VerifiedAgentSession,
    parse_agent_session_headers,
)
from cti_app.application.agentic_note_tools import (
    AgenticNoteCaptureService,
    NoteCaptureUnitOfWork,
)
from cti_app.application.agentic_tool_broker import (
    AgenticBrokerError,
    AgenticToolBroker,
)
from cti_app.application.investigation_handoffs import HandoffError, InvestigationHandoffService
from cti_app.application.investigation_tasks import TaskCommandError
from cti_app.application.operator_tool_invocation import OperatorToolInvocationError
from cti_app.application.work_contributions import (
    WorkContributionService,
    WorkContributionUnitOfWork,
)
from cti_app.application.workspace_agent import WorkspaceAgentPort
from cti_app.config import Settings, get_settings
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient

router = APIRouter(prefix="/api/investigations", tags=["agentic-tools"])


class AgenticToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    schema_version: str
    arguments: dict[str, Any]


class AgenticToolCallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agentic-tool-call.v1"] = "agentic-tool-call.v1"
    investigation_id: UUID
    session_id: UUID
    task_id: UUID
    tool_name: str
    status: str
    idempotent_replay: bool
    invocation_id: UUID | None
    output: dict[str, Any]
    error_code: str | None


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _session_verifier(request: Request) -> AgentSessionVerifier:
    """The mounted verifier, or the real HMAC one built from settings.

    An application that wires ``app.state.agent_session_verifier`` (the
    orchestrator does this at integration time) takes precedence; otherwise the
    real verifier is built per request exactly like lot 05B1's session routes.
    A missing ``M24_AGENT_SESSION_KEY`` reaches the typed 503 refusal.
    """
    configured = getattr(request.app.state, "agent_session_verifier", None)
    if configured is not None:
        return cast(AgentSessionVerifier, configured)
    key = _settings(request).m24_agent_session_key
    uow_factory = cast(Callable[[], AgentSessionVerificationUnitOfWork], _uow_factory(request))
    return HmacAgentSessionVerifier(
        uow_factory=uow_factory,
        deriver=HmacSessionSecretDeriver(key) if key is not None else None,
    )


def _path_and_query(request: Request) -> str:
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


async def require_signed_agent_session(
    request: Request,
    *,
    investigation_id: UUID,
    task_id: UUID | None = None,
    require_current_lease: bool = False,
    expected_session_id: UUID | None = None,
) -> VerifiedAgentSession:
    """Verify the D7 signature of the current request or raise its typed refusal.

    The raw body is signed, so callers must invoke this after FastAPI has read
    the body (Starlette caches it) and before trusting any parsed field.
    """
    raw_session = request.headers.get("x-autowork-agent-session")
    if raw_session is not None:
        try:
            UUID(raw_session.strip())
        except ValueError:
            # Keep lot 04B3's stable refusal for a malformed session header.
            raise _http_error(403, "agent_session_required") from None
    try:
        session_id, timestamp, signature = parse_agent_session_headers(request.headers)
    except AgentSessionVerificationError as exc:
        raise _http_error(exc.status_code, exc.code, str(exc), details=exc.details or None) from exc
    if expected_session_id is not None and session_id != expected_session_id:
        raise _http_error(403, "agent_session_binding_mismatch")
    body = await request.body()
    try:
        return await _session_verifier(request).verify_session_request(
            method=request.method,
            path_and_query=_path_and_query(request),
            session_id=session_id,
            timestamp=timestamp,
            signature=signature,
            body=body,
            idempotency_key=request.headers.get("idempotency-key"),
            investigation_id=investigation_id,
            task_id=task_id,
            require_current_lease=require_current_lease,
        )
    except AgentSessionVerificationError as exc:
        raise _http_error(exc.status_code, exc.code, str(exc), details=exc.details or None) from exc


def _composition_factory(request: Request) -> Any:
    factory = getattr(request.app.state, "investigation_composition_factory", None)
    if factory is None:
        raise _http_error(503, "investigation_unavailable")
    return factory


def _workspace_agent(request: Request) -> tuple[WorkspaceAgentPort, bool]:
    """The injected host agent, or the configured UDS client (closed if ours)."""
    injected = getattr(request.app.state, "workspace_agent_port", None)
    if injected is not None:
        return cast(WorkspaceAgentPort, injected), False
    settings = _settings(request)
    return (
        HttpWorkspaceAgentClient(
            socket_path=settings.workspace_agent_socket or None,
            token_file=settings.workspace_agent_token_file or None,
        ),
        True,
    )


def _contribution_service(
    request: Request, factory: Any, agent: WorkspaceAgentPort
) -> WorkContributionService | None:
    """The single M24-07 import writer, when the composition exposes its stores."""
    artifact_store = getattr(factory, "artifact_store", None)
    article_snapshots = getattr(factory, "article_snapshots", None)
    if artifact_store is None or article_snapshots is None:
        return None
    return WorkContributionService(
        uow_factory=cast(Callable[[], WorkContributionUnitOfWork], _uow_factory(request)),
        workspace_agent=agent,
        artifact_store=artifact_store,
        article_snapshots=article_snapshots,
        max_contribution_bytes=_settings(request).task_limits().max_contribution_bytes,
    )


def _note_service(request: Request, agent: WorkspaceAgentPort) -> AgenticNoteCaptureService:
    return AgenticNoteCaptureService(
        uow_factory=cast(Callable[[], NoteCaptureUnitOfWork], _uow_factory(request)),
        workspace_agent=agent,
    )


def _operator_error(
    exc: AgenticBrokerError | OperatorToolInvocationError | TaskCommandError | HandoffError,
) -> Any:
    return _http_error(
        exc.status_code,
        exc.code,
        str(exc),
        details=getattr(exc, "details", None) or None,
    )


@router.post(
    "/{investigation_id}/agent-sessions/{session_id}/tool-calls",
    response_model=AgenticToolCallResponse,
)
async def call_agentic_tool(
    investigation_id: UUID,
    session_id: UUID,
    payload: AgenticToolCallRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> AgenticToolCallResponse:
    verified = await require_signed_agent_session(
        request,
        investigation_id=investigation_id,
        expected_session_id=session_id,
        require_current_lease=True,
    )
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        factory = _composition_factory(request)
        composition = factory.build(uow, investigation_id, operator_mode=True)
        handoffs = InvestigationHandoffService(uow_factory=cast(Any, _uow_factory(request)))
        commit = getattr(uow, "commit", None)
        agent, should_close = _workspace_agent(request)
        try:
            broker = AgenticToolBroker(
                investigation_id=investigation_id,
                prose_authority=investigation.prose_authority,
                uow=cast(Any, uow),
                invocations=composition.operator_tool_invocations,
                handoffs=handoffs,
                contributions=_contribution_service(request, factory, agent),
                notes=_note_service(request, agent),
                task_limits=_settings(request).task_limits(),
                commit=cast(Callable[[], Any], commit) if callable(commit) else None,
            )
            try:
                result = await broker.invoke(
                    session=verified,
                    tool_name=payload.tool_name,
                    schema_version=payload.schema_version,
                    arguments=payload.arguments,
                    client_idempotency_key=idempotency_key,
                )
            except (
                AgenticBrokerError,
                OperatorToolInvocationError,
                TaskCommandError,
                HandoffError,
            ) as exc:
                raise _operator_error(exc) from exc
        finally:
            if should_close:
                await agent.aclose()
    return AgenticToolCallResponse(
        investigation_id=result.investigation_id,
        session_id=result.session_id,
        task_id=result.task_id,
        tool_name=result.tool_name,
        status=result.status,
        idempotent_replay=result.idempotent_replay,
        invocation_id=result.invocation_id,
        output=dict(result.output),
        error_code=result.error_code,
    )


__all__ = ["require_signed_agent_session", "router"]

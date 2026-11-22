"""Agent-session routes (M24-05B1, ADR-0011 D7/D8).

The orchestrator owns ``api/main.py``; this module only exports ``router`` and
never registers itself.  Two authentication modes coexist:

* Human actions use the canonical verified-human gate
  (``_require_verified_human_operator``): open a session, interrupt, resume.
* Operator actions use the D7 per-session HMAC headers, because the operator
  is a process that holds only the one-time session secret.  The shared
  ``OPERATOR_CLIENT`` token alone can never authorize a write here; the
  signature is verified before any body is trusted.

``POST /agent-sessions`` performs prepare *and* start atomically: a stale
handoff refuses before anything persists.  The response contains the session
secret exactly once; every other representation omits it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cti_app.api.investigation import (
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.agent_session_verification import (
    AgentSessionVerificationError,
    AgentSessionVerificationUnitOfWork,
    AgentSessionVerifier,
    HmacAgentSessionVerifier,
    HmacSessionSecretDeriver,
    VerifiedAgentSession,
    parse_agent_session_headers,
)
from cti_app.application.agentic_sessions import (
    AgentSessionSecretDeliveryPort,
    PreparedAgentSession,
    SessionError,
    SessionService,
    SessionUnitOfWork,
)
from cti_app.application.investigation_handoffs import HandoffError
from cti_app.application.investigation_tasks import TaskCommandError
from cti_app.config import Settings, get_settings
from cti_app.domain.agentic_operator import OperatorAdapterKind
from cti_app.domain.investigation_task import TaskActor, TaskActorKind

router = APIRouter(prefix="/api/investigations", tags=["agent-sessions"])


class OpenAgentSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    adapter_kind: OperatorAdapterKind
    handoff_id: UUID
    executable: str = Field(min_length=1, max_length=4096)


class InstructionRequest(BaseModel):
    """Enqueue is a human action; ack is the owning operator's signed action."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["enqueue", "ack"]
    text: str | None = Field(default=None, max_length=65536)
    instruction_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def _validate_action_shape(self) -> InstructionRequest:
        if self.action == "enqueue":
            if self.text is None or not self.text.strip():
                raise ValueError("enqueue requires a non-empty instruction text")
            if self.instruction_ids:
                raise ValueError("enqueue does not acknowledge instruction ids")
        else:
            if self.text is not None:
                raise ValueError("ack does not carry instruction text")
            if not self.instruction_ids:
                raise ValueError("ack requires at least one instruction id")
        return self


class InterruptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=4096)


class CollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str | None = Field(default=None, max_length=1024)


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _deriver(request: Request) -> HmacSessionSecretDeriver | None:
    key = _settings(request).m24_agent_session_key
    return HmacSessionSecretDeriver(key) if key is not None else None


def _operator_port(request: Request) -> Any:
    return getattr(request.app.state, "agentic_operator_port", None)


def _secret_delivery(request: Request) -> AgentSessionSecretDeliveryPort | None:
    return cast(
        AgentSessionSecretDeliveryPort | None,
        getattr(request.app.state, "agent_session_secret_delivery", None),
    )


def _service(request: Request) -> SessionService:
    uow_factory = cast(Callable[[], SessionUnitOfWork], _uow_factory(request))
    return SessionService(
        uow_factory=uow_factory,
        deriver=_deriver(request),
        operator=_operator_port(request),
        secret_delivery=_secret_delivery(request),
        task_limits=_settings(request).task_limits(),
    )


def _verifier(request: Request) -> AgentSessionVerifier:
    uow_factory = cast(Callable[[], AgentSessionVerificationUnitOfWork], _uow_factory(request))
    return HmacAgentSessionVerifier(
        uow_factory=uow_factory,
        deriver=_deriver(request),
    )


def _session_http_error(
    exc: SessionError | AgentSessionVerificationError | HandoffError | TaskCommandError,
) -> HTTPException:
    details = getattr(exc, "details", None)
    return _http_error(
        exc.status_code,
        exc.code,
        str(exc),
        details=cast(Mapping[str, Any] | None, details) or None,
    )


async def _human_actor(request: Request) -> TaskActor:
    identity = await _require_verified_human_operator(request)
    return TaskActor(kind=TaskActorKind.HUMAN, actor_id=identity.actor_id)


def _path_and_query(request: Request) -> str:
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def _has_session_headers(request: Request) -> bool:
    return bool(request.headers.get("x-autowork-agent-session"))


def _signed_session_id(request: Request) -> UUID:
    try:
        session_id, _timestamp, _signature = parse_agent_session_headers(
            request.headers, now=datetime.now(UTC)
        )
    except AgentSessionVerificationError as exc:
        raise _session_http_error(exc) from exc
    return session_id


async def _verified_operator_call(
    request: Request,
    *,
    investigation_id: UUID,
    session_id: UUID,
    require_current_lease: bool,
) -> VerifiedAgentSession:
    try:
        signed_session, timestamp, signature = parse_agent_session_headers(
            request.headers, now=datetime.now(UTC)
        )
    except AgentSessionVerificationError as exc:
        raise _session_http_error(exc) from exc
    if signed_session != session_id:
        raise _http_error(403, "agent_session_binding_mismatch")
    body = await request.body()
    try:
        return await _verifier(request).verify_session_request(
            method=request.method,
            path_and_query=_path_and_query(request),
            session_id=signed_session,
            timestamp=timestamp,
            signature=signature,
            body=body,
            idempotency_key=request.headers.get("idempotency-key"),
            investigation_id=investigation_id,
            require_current_lease=require_current_lease,
        )
    except AgentSessionVerificationError as exc:
        raise _session_http_error(exc) from exc


async def _operator_actor(request: Request, verified: VerifiedAgentSession) -> TaskActor:
    """The owning operator identity from the task assignment (never client text)."""
    factory = cast(Callable[[], SessionUnitOfWork], _uow_factory(request))
    async with factory() as uow:
        task = await uow.investigation_tasks.get(verified.task_id)
    if task is None or task.assigned_actor_kind is not TaskActorKind.OPERATOR:
        raise _http_error(403, "agent_session_binding_mismatch")
    if task.assigned_actor_id is None:
        raise _http_error(403, "agent_session_binding_mismatch")
    return TaskActor(kind=TaskActorKind.OPERATOR, actor_id=task.assigned_actor_id)


def _session_response(prepared: PreparedAgentSession) -> dict[str, Any]:
    """Public session fields only; the secret went to the host agent (D7)."""
    return {"session": prepared.session.to_payload()}


@router.get("/{investigation_id}/agent-sessions")
async def list_agent_sessions(
    investigation_id: UUID, request: Request, task_id: UUID | None = None
) -> list[dict[str, Any]]:
    if _has_session_headers(request):
        await _verified_operator_call(
            request,
            investigation_id=investigation_id,
            session_id=_signed_session_id(request),
            require_current_lease=False,
        )
    else:
        await _human_actor(request)
    try:
        sessions = await _service(request).list(investigation_id, task_id=task_id)
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    return [session.to_payload() for session in sessions]


@router.post("/{investigation_id}/agent-sessions")
async def open_agent_session(
    investigation_id: UUID,
    body: OpenAgentSessionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> dict[str, Any]:
    actor = await _human_actor(request)
    try:
        prepared = await _service(request).open_session(
            investigation_id,
            task_id=body.task_id,
            adapter_kind=body.adapter_kind,
            handoff_id=body.handoff_id,
            executable=body.executable,
            actor=actor,
            idempotency_key=idempotency_key,
        )
    except (SessionError, HandoffError, TaskCommandError) as exc:
        raise _session_http_error(exc) from exc
    return _session_response(prepared)


@router.get("/{investigation_id}/agent-sessions/{session_id}")
async def get_agent_session(
    investigation_id: UUID, session_id: UUID, request: Request
) -> dict[str, Any]:
    if _has_session_headers(request):
        await _verified_operator_call(
            request,
            investigation_id=investigation_id,
            session_id=session_id,
            require_current_lease=False,
        )
    else:
        await _human_actor(request)
    try:
        session = await _service(request).get(investigation_id, session_id)
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    return session.to_payload()


@router.post("/{investigation_id}/agent-sessions/{session_id}/instructions")
async def send_instruction(
    investigation_id: UUID, session_id: UUID, body: InstructionRequest, request: Request
) -> dict[str, Any] | list[dict[str, Any]]:
    if body.action == "enqueue":
        actor = await _human_actor(request)
        idempotency_key = request.headers.get("idempotency-key", "").strip()
        if not idempotency_key:
            raise _http_error(422, "idempotency_key_required")
        try:
            instruction = await _service(request).enqueue_instruction(
                investigation_id,
                session_id,
                text=cast(str, body.text),
                actor=actor,
                idempotency_key=idempotency_key,
            )
        except SessionError as exc:
            raise _session_http_error(exc) from exc
        return instruction.to_payload()
    verified = await _verified_operator_call(
        request,
        investigation_id=investigation_id,
        session_id=session_id,
        require_current_lease=True,
    )
    actor = await _operator_actor(request, verified)
    try:
        acknowledged = await _service(request).acknowledge_instructions(
            investigation_id,
            session_id,
            instruction_ids=body.instruction_ids,
            actor=actor,
        )
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    return [instruction.to_payload() for instruction in acknowledged]


@router.post("/{investigation_id}/agent-sessions/{session_id}/interrupt")
async def interrupt_agent_session(
    investigation_id: UUID, session_id: UUID, body: InterruptRequest, request: Request
) -> dict[str, Any]:
    actor = await _human_actor(request)
    try:
        session = await _service(request).interrupt(
            investigation_id, session_id, reason=body.reason, actor=actor
        )
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    return session.to_payload()


@router.post("/{investigation_id}/agent-sessions/{session_id}/resume")
async def resume_agent_session(
    investigation_id: UUID, session_id: UUID, request: Request
) -> dict[str, Any]:
    actor = await _human_actor(request)
    try:
        session = await _service(request).resume(investigation_id, session_id, actor=actor)
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    return session.to_payload()


@router.post("/{investigation_id}/agent-sessions/{session_id}/reconcile")
async def reconcile_agent_session(
    investigation_id: UUID, session_id: UUID, request: Request
) -> dict[str, Any]:
    verified = await _verified_operator_call(
        request,
        investigation_id=investigation_id,
        session_id=session_id,
        require_current_lease=False,
    )
    actor = await _operator_actor(request, verified)
    try:
        session = await _service(request).reconcile(investigation_id, session_id, actor=actor)
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    return session.to_payload()


@router.post("/{investigation_id}/agent-sessions/{session_id}/collect")
async def collect_agent_results(
    investigation_id: UUID, session_id: UUID, request: Request
) -> dict[str, Any]:
    # The D7 signature covers the raw bytes, so it is verified before the body
    # is parsed into a model; a malformed body never reaches the service.
    verified = await _verified_operator_call(
        request,
        investigation_id=investigation_id,
        session_id=session_id,
        require_current_lease=False,
    )
    actor = await _operator_actor(request, verified)
    try:
        collect_request = CollectRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        raise _http_error(422, "invalid_collect_request") from exc
    try:
        results = await _service(request).collect(
            investigation_id, session_id, actor=actor, cursor=collect_request.cursor
        )
    except SessionError as exc:
        raise _session_http_error(exc) from exc
    return results.model_dump(mode="json")


__all__ = ["router"]

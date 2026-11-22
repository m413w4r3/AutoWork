"""Investigation handoff routes (M24-05A, ADR-0011 D5/D8).

The orchestrator owns ``api/main.py``; this module only exports ``router`` and
never registers itself.

Actor seam: D8 admits a verified human and the *owning operator* of a task.
An operator request is authenticated only by the D7 session signature (lot
05B2 reconciliation of the 05B1 gate); the shared ``OPERATOR_CLIENT`` token
alone is refused with the typed ``403 agent_session_required``.  The service
still enforces the owning-operator rule for handoff creation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from cti_app.api.agentic_tools import require_signed_agent_session
from cti_app.api.investigation import (
    _http_error,
    _identity,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.identity import IdentityScope
from cti_app.application.investigation_handoffs import (
    HandoffCreateRequest,
    HandoffError,
    HandoffUnitOfWork,
    InvestigationHandoffService,
)
from cti_app.domain.investigation_handoff import HandoffActor, HandoffActorKind

router = APIRouter(prefix="/api/investigations", tags=["investigation-handoffs"])


def _service(request: Request) -> InvestigationHandoffService:
    uow_factory = cast(Callable[[], HandoffUnitOfWork], _uow_factory(request))
    return InvestigationHandoffService(uow_factory=uow_factory)


def _handoff_http_error(exc: HandoffError) -> HTTPException:
    return _http_error(
        exc.status_code,
        exc.code,
        str(exc),
        details=exc.details or None,
    )


async def _handoff_actor(
    request: Request,
    investigation_id: UUID,
    *,
    task_id: UUID | None = None,
    require_current_lease: bool = False,
) -> HandoffActor:
    """Verified human, or the D7-signed owning operator of the task."""
    if request.headers.get("x-autowork-agent-session"):
        verified = await require_signed_agent_session(
            request,
            investigation_id=investigation_id,
            task_id=task_id,
            require_current_lease=require_current_lease,
        )
        return HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=verified.operator_id)
    identity = await _identity(request)
    if identity.has_scope(IdentityScope.OPERATOR_CLIENT):
        raise _http_error(403, "agent_session_required")
    verified_human = await _require_verified_human_operator(request)
    return HandoffActor(kind=HandoffActorKind.HUMAN, actor_id=verified_human.actor_id)


@router.get("/{investigation_id}/handoffs")
async def list_handoffs(
    investigation_id: UUID, request: Request, task_id: UUID | None = None
) -> list[dict[str, Any]]:
    await _handoff_actor(request, investigation_id)
    try:
        records = await _service(request).list(investigation_id, task_id=task_id)
    except HandoffError as exc:
        raise _handoff_http_error(exc) from exc
    return [record.handoff.to_payload() for record in records]


@router.post("/{investigation_id}/handoffs")
async def create_handoff(
    investigation_id: UUID,
    body: HandoffCreateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> dict[str, Any]:
    actor = await _handoff_actor(
        request,
        investigation_id,
        task_id=body.task_id,
        require_current_lease=True,
    )
    try:
        record = await _service(request).create(
            investigation_id,
            actor=actor,
            idempotency_key=idempotency_key,
            request=body,
        )
    except HandoffError as exc:
        raise _handoff_http_error(exc) from exc
    return record.handoff.to_payload()


@router.get("/{investigation_id}/handoffs/{handoff_id}")
async def get_handoff(investigation_id: UUID, handoff_id: UUID, request: Request) -> dict[str, Any]:
    await _handoff_actor(request, investigation_id)
    try:
        record = await _service(request).get(investigation_id, handoff_id)
    except HandoffError as exc:
        raise _handoff_http_error(exc) from exc
    return record.handoff.to_payload()


@router.get("/{investigation_id}/handoffs/{handoff_id}/freshness")
async def get_handoff_freshness(
    investigation_id: UUID, handoff_id: UUID, request: Request
) -> dict[str, Any]:
    await _handoff_actor(request, investigation_id)
    try:
        report = await _service(request).freshness(investigation_id, handoff_id)
    except HandoffError as exc:
        raise _handoff_http_error(exc) from exc
    return report.model_dump(mode="json")


__all__ = ["router"]

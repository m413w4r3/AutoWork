"""Task-scoped Bridge consultations (M24.1 F4-D B3).

A verified human asks Bridge to interpret, critique or propose pivots for one
persisted task.  The route reuses the single ``advisor.consult`` path: it
assembles and sanitises a bounded, non-evidence ``task_context`` from the task
and its human contributions, invokes the advisor tool exactly once with a human
requester and ``context_task_id`` (migration 0028), then projects the persisted
ledger row.  It never writes a task status, a task result, a proof or a
decision.

The orchestrator owns ``api/main.py``: this module only exports ``router``.
``application.investigation_task_context`` is shared with the optional
``context_task_id`` on ``POST /plans/request`` so both routes send the same
sanitised dossier.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.investigation_advisor_tools import ADVISOR_SCHEMA_VERSION
from cti_app.application.investigation_consultations import (
    InvestigationConsultationV1,
    consultation_ledger,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_state import InvestigationStateVersionConflict
from cti_app.application.investigation_task_context import (
    TaskContextError,
    build_task_context,
    task_context_arguments,
)
from cti_app.application.operator_tool_invocation import OperatorToolInvocationError
from cti_app.domain.investigation import ToolInvocationActorKind

router = APIRouter(prefix="/api/investigations", tags=["investigation-task-consultations"])

_ADVISOR_TOOL_NAME = "advisor.consult"
_PURPOSE = "Demande humaine d'une consultation Bridge ciblée sur une tâche d'enquête (M24)."


class TaskConsultationRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consultation_type: Literal["interpretation", "critique", "pivot_strategy"]
    question: str = Field(min_length=1, max_length=8_000)
    include_previous_consultation: bool = False
    anchor: str | None = Field(default=None, min_length=1, max_length=128)
    expected_working_state_version: int = Field(ge=1)
    expected_working_state_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class TaskConsultationResponseV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: UUID
    invocation_status: str
    context_task_id: UUID
    task_context_sha256: str
    consultation: InvestigationConsultationV1


def _invocation_idempotency_prefix(investigation_id: UUID, idempotency_key: str) -> str:
    """The operation prefix the invocation service derives for one request key.

    A replay must rebuild the same arguments, so the optional previous
    consultation excludes the invocation produced by this very request.
    """
    digest = hashlib.sha256(f"{investigation_id}\0{idempotency_key}".encode()).hexdigest()
    return f"operator:{digest}:"


@router.post(
    "/{investigation_id}/tasks/{task_id}/consultations",
    response_model=TaskConsultationResponseV1,
)
async def request_task_consultation(
    investigation_id: UUID,
    task_id: UUID,
    payload: TaskConsultationRequestV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> TaskConsultationResponseV1:
    identity = await _require_verified_human_operator(request)
    factory = _composition_factory(request)
    question = payload.question.strip()
    if not question:
        raise _http_error(422, "task_consultation_question_required")
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        task = await uow.investigation_tasks.get(task_id)
        if task is None or task.investigation_id != investigation_id:
            raise _http_error(404, "task_not_found")
        if task.is_terminal:
            # A consultation never revives a terminal task; it is only context.
            raise _http_error(422, "task_consultation_task_terminal")
        idempotency_prefix = _invocation_idempotency_prefix(investigation_id, idempotency_key)
        try:
            task_context = await build_task_context(
                uow=uow,
                task=task,
                factory=factory,
                investigation=investigation,
                include_previous_consultation=payload.include_previous_consultation,
                anchor=payload.anchor,
                idempotency_prefix=idempotency_prefix,
            )
        except TaskContextError as exc:
            raise _http_error(422, exc.code) from exc
        if task_context.context is None or task_context.sha256 is None:
            raise _http_error(422, "task_consultation_context_invalid")
        arguments = {
            "consultation_type": payload.consultation_type,
            "question": question,
            **task_context_arguments(task_context),
        }
        composition = factory.build(uow, investigation_id, operator_mode=True)
        try:
            invocation_result = await composition.operator_tool_invocations.invoke(
                tool_name=_ADVISOR_TOOL_NAME,
                schema_version=ADVISOR_SCHEMA_VERSION,
                arguments=arguments,
                purpose=_PURPOSE,
                expected_version=payload.expected_working_state_version,
                expected_sha256=payload.expected_working_state_sha256,
                idempotency_key=idempotency_key,
                actor_id=identity.actor_id,
                requester_actor_kind=ToolInvocationActorKind.HUMAN,
                context_task_id=task.id,
            )
        except OperatorToolInvocationError as exc:
            raise _http_error(exc.status_code, exc.code, details=exc.details) from exc
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "stale_working_state") from exc
        entries = await consultation_ledger(uow, factory.artifact_store).list_for_investigation(
            investigation_id, context_task_id=task.id
        )
        entry = next(
            (item for item in entries if item.invocation_id == invocation_result.invocation.id),
            None,
        )
        if entry is None:
            raise _http_error(409, "task_consultation_not_projected")
        await uow.commit()
        return TaskConsultationResponseV1(
            invocation_id=invocation_result.invocation.id,
            invocation_status=invocation_result.invocation.status.value,
            context_task_id=task.id,
            task_context_sha256=task_context.sha256,
            consultation=entry,
        )


__all__ = ["router"]

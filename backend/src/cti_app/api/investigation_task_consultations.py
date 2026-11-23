"""Task-scoped Bridge consultations (M24.1 F4-D B3).

A verified human asks Bridge to interpret, critique or propose pivots for one
persisted task.  The route reuses the single ``advisor.consult`` path: it
assembles a bounded, non-evidence ``task_context`` from the task and its human
contributions, invokes the advisor tool exactly once with a human requester and
``context_task_id`` (migration 0028), then projects the persisted ledger row.
It never writes a task status, a task result, a proof or a decision.

The orchestrator owns ``api/main.py``: this module only exports ``router``.
``assemble_task_context`` is shared with the optional ``context_task_id`` on
``POST /plans/request`` so both routes send the same dossier.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_SCHEMA_VERSION,
    _sanitize_advisor_task_context,
)
from cti_app.application.investigation_consultations import (
    InvestigationConsultationLedger,
    InvestigationConsultationV1,
)
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    ToolInvocationRepository,
    VersionConflictError,
)
from cti_app.application.investigation_state import InvestigationStateVersionConflict
from cti_app.application.operator_tool_invocation import OperatorToolInvocationError
from cti_app.domain.investigation import (
    ToolInvocationActorKind,
    canonical_json_bytes,
)
from cti_app.domain.investigation_task import InvestigationTaskV1

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


def _ledger(uow: Any, factory: Any) -> InvestigationConsultationLedger:
    return InvestigationConsultationLedger(
        invocations=cast(ToolInvocationRepository, uow.tool_invocations),
        observations=cast(EvidenceObservationRepository, uow.evidence_observations),
        artifact_store=factory.artifact_store,
        decisions=uow.analyst_decisions,
    )


async def _editorial_anchor(
    factory: Any,
    investigation: Any,
    anchor: str,
) -> dict[str, str]:
    """Resolve one canonical publication anchor to its bounded excerpt.

    The article snapshot exposes the renderable anchors with a bounded excerpt;
    ``text_sha256`` identifies the exact excerpt transmitted to Bridge.
    """
    snapshot = await factory.article_snapshots.resolve(investigation.synthesis_artifact_id)
    entry = next(
        (item for item in snapshot.article.publication_anchors if item.anchor == anchor),
        None,
    )
    if entry is None:
        raise _http_error(
            422,
            "task_consultation_anchor_invalid",
            "The requested anchor is not a renderable anchor of the current article",
        )
    excerpt = entry.excerpt
    return {
        "anchor": entry.anchor,
        "excerpt": excerpt,
        "text_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }


def _task_contributions(contributions: Any, task_id: UUID) -> list[dict[str, str]]:
    rows = [
        {
            "id": str(item.id),
            "category": item.category.value,
            "file_relpath": item.file_relpath,
            "file_sha256": item.file_sha256,
            "origin": item.origin.value,
            "import_status": item.import_status.value,
        }
        for item in contributions
        if item.task_id == task_id
    ]
    # The invocation payload is idempotent: keep the list order deterministic.
    return sorted(rows, key=lambda row: (row["id"],))


async def assemble_task_context(
    *,
    uow: Any,
    task: InvestigationTaskV1,
    factory: Any,
    investigation: Any,
    include_previous_consultation: bool = False,
    anchor: str | None = None,
    idempotency_prefix: str | None = None,
) -> dict[str, Any]:
    """Assemble the bounded non-evidence context of one human task request."""
    contributions = await uow.work_contributions.list_for_investigation(task.investigation_id)
    context: dict[str, Any] = {
        "investigation_id": str(task.investigation_id),
        "task_id": str(task.id),
        "task_key": task.task_key,
        "title": task.title,
        "question": task.question,
        "objective": task.objective,
        "status": task.status.value,
        "priority": task.priority.value,
        "claim_refs": list(task.claim_refs),
        "evidence_refs": list(task.evidence_refs),
        "sample_refs": list(task.sample_refs),
        "expected_outputs": list(task.expected_outputs),
        "result_refs": [dict(item) for item in task.result_refs],
        "human_contributions": _task_contributions(contributions, task.id),
    }
    if task.assigned_actor_kind is not None:
        context["assigned_actor"] = {
            "kind": task.assigned_actor_kind.value,
            "actor_id": task.assigned_actor_id,
        }
    if include_previous_consultation:
        entries = await _ledger(uow, factory).list_for_investigation(
            task.investigation_id, context_task_id=task.id
        )
        prior = [
            entry
            for entry in entries
            if idempotency_prefix is None or not entry.operation_key.startswith(idempotency_prefix)
        ]
        if prior:
            last = prior[-1]
            context["previous_consultation"] = {
                "invocation_id": str(last.invocation_id),
                "type": last.consultation_type,
                "output_sha256": last.output_sha256,
                "parse_status": last.parse_status.value,
            }
    if anchor is not None:
        context["editorial_anchor"] = await _editorial_anchor(factory, investigation, anchor)
    return context


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
        task_context = await assemble_task_context(
            uow=uow,
            task=task,
            factory=factory,
            investigation=investigation,
            include_previous_consultation=payload.include_previous_consultation,
            anchor=payload.anchor,
            idempotency_prefix=idempotency_prefix,
        )
        sanitized_task_context = _sanitize_advisor_task_context(
            task_context, investigation_id=investigation_id, warnings=[]
        )
        if sanitized_task_context is None:
            raise _http_error(422, "task_consultation_context_invalid")
        task_context_sha256 = hashlib.sha256(
            canonical_json_bytes(sanitized_task_context)
        ).hexdigest()
        arguments = {
            "consultation_type": payload.consultation_type,
            "question": question,
            "task_context": task_context,
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
        entries = await _ledger(uow, factory).list_for_investigation(
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
            task_context_sha256=task_context_sha256,
            consultation=entry,
        )


__all__ = ["assemble_task_context", "router"]

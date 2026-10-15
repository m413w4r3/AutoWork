"""Read-only operator projection for durable investigation checkpoints."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.application.bridge_investigator import (
    make_investigation_conversation_id,
    make_investigation_turn_idempotency_key,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1
from cti_app.domain.investigation import (
    BudgetDimension,
    InvestigationEnvelope,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.investigation_state import InvestigationWorkingState, StopReason
from cti_app.domain.jobs import Job, JobStatus
from cti_app.domain.production import AnalystInvestigation


class InvestigationOperatorErrorCode(StrEnum):
    INVESTIGATION_NOT_FOUND = "investigation_not_found"
    INVESTIGATION_STATE_MISSING = "investigation_not_started"
    STATUS_UNAVAILABLE = "investigation_operator_status_unavailable"


class OperatorNextAction(StrEnum):
    WAIT_FOR_WORKER = "wait_for_worker"
    INSPECT_BRIDGE_TURN = "inspect_bridge_turn"
    RECONCILE_TOOL_INVOCATION = "reconcile_tool_invocation"
    REQUEST_G2_DECISION = "request_g2_decision"
    RESUME_INVESTIGATION = "resume_investigation"
    NO_ACTION = "no_action"


@dataclass(frozen=True, slots=True)
class InvestigationOperatorView:
    payload: dict[str, Any]


def build_investigation_operator_view(
    *,
    investigation: AnalystInvestigation,
    state: InvestigationWorkingState,
    synthesis_sha256: str,
    envelope: InvestigationEnvelope | None,
    invocations: tuple[ToolInvocation, ...],
    observations: tuple[Any, ...],
    opinion: AnalystOpinionV1 | None,
    jobs: tuple[Job, ...],
    available_tools: tuple[str, ...],
    unavailable_tools: tuple[str, ...],
) -> InvestigationOperatorView:
    ordered_jobs = tuple(sorted(jobs, key=lambda item: (item.created_at, item.id.hex)))
    generation = max(
        (int(job.input_parameters.get("run_generation", 0)) for job in ordered_jobs),
        default=0,
    )
    active_jobs = tuple(
        job for job in ordered_jobs if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
    )
    ordered_invocations = tuple(
        sorted(invocations, key=lambda item: (item.updated_at, item.created_at, item.id.hex))
    )
    unresolved = tuple(
        item
        for item in ordered_invocations
        if item.status
        in {
            ToolInvocationStatus.PENDING,
            ToolInvocationStatus.RUNNING,
            ToolInvocationStatus.NEEDS_REVIEW,
        }
        and not item.response_refs
    )
    ambiguous = tuple(
        item
        for item in unresolved
        if item.status in {ToolInvocationStatus.RUNNING, ToolInvocationStatus.NEEDS_REVIEW}
    )
    latest_invocation = ordered_invocations[-1] if ordered_invocations else None
    pending_bridge_key = None
    bridge_conversation_id = make_investigation_conversation_id(investigation.id)
    if state.pending_context_sha256 is not None:
        pending_bridge_key = make_investigation_turn_idempotency_key(
            investigation_id=investigation.id,
            turn_index=state.turn_index,
            context_sha256=state.pending_context_sha256,
        )

    if pending_bridge_key is not None:
        last_operation: dict[str, Any] | None = {
            "kind": "bridge_turn",
            "id": pending_bridge_key,
            "conversation_id": bridge_conversation_id,
            "turn_index": state.turn_index,
            "context_snapshot_id": state.pending_context_snapshot_id,
            "context_sha256": state.pending_context_sha256,
            "status": (
                "needs_review"
                if state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
                else "pending_or_running"
            ),
            "evidence_missing": ["persisted Bridge turn result or provider outcome"],
            "decision_expected": "inspect persisted turn, then reconcile uncertain transport state",
        }
    elif latest_invocation is not None:
        last_operation = _invocation_view(latest_invocation)
    else:
        last_operation = None

    dimensions = envelope.budget if envelope is not None else None
    fallback_limits = {
        BudgetDimension.BRIDGE_TURNS: investigation.budget.max_cycles,
        BudgetDimension.VT_UNITS: 0,
        BudgetDimension.SHODAN_CREDITS: 0,
        BudgetDimension.DOWNLOADS: 0,
    }
    budget = {
        dimension.value: {
            "limit": (
                dimensions.limits.get(dimension) if dimensions else fallback_limits.get(dimension)
            ),
            "consumed": dimensions.consumed[dimension] if dimensions else 0,
            "reserved": dimensions.reserved[dimension] if dimensions else 0,
            "remaining": dimensions.remaining[dimension] if dimensions else None,
        }
        for dimension in BudgetDimension
    }
    reservations = (
        [
            {
                "operation_key": item.operation_key,
                "status": item.status.value,
                "amounts": {key.value: value for key, value in item.amounts.items()},
                "committed_amounts": {
                    key.value: value for key, value in item.committed_amounts.items()
                },
            }
            for item in dimensions.reservations
        ]
        if dimensions is not None
        else []
    )
    evidence = [
        {"id": item.id, "sha256": item.sha256, "invocation_id": item.invocation_ref}
        for item in sorted(observations, key=lambda value: (value.observed_at, value.id.hex))
    ]
    evidence_identity = [
        {
            "id": str(item["id"]),
            "sha256": item["sha256"],
            "invocation_id": str(item["invocation_id"]) if item["invocation_id"] else None,
        }
        for item in evidence
    ]
    evidence_checkpoint_sha256 = hashlib.sha256(
        canonical_json_bytes({"observations": evidence_identity})
    ).hexdigest()
    opinion_checkpoint = (
        {
            "id": opinion.id,
            "version": opinion.version,
            "content_sha256": opinion.content_sha256,
            "status": opinion.status.value,
        }
        if opinion is not None
        else None
    )

    next_action = _next_action(
        investigation=investigation,
        state=state,
        generation=generation,
        active_jobs=active_jobs,
        unresolved=unresolved,
        pending_bridge_key=pending_bridge_key,
        bridge_conversation_id=bridge_conversation_id,
        envelope_version=envelope.version if envelope else 0,
    )
    return InvestigationOperatorView(
        payload={
            "investigation_id": investigation.id,
            "subject_id": investigation.subject_id,
            "production_run_id": investigation.production_run_id,
            "synthesis_artifact_id": investigation.synthesis_artifact_id,
            "synthesis_sha256": synthesis_sha256,
            "base_publication_artifact_id": state.base_publication_artifact_id,
            "base_publication_sha256": state.base_publication_sha256,
            "investigation_status": investigation.status.value,
            "investigation_version": investigation.version,
            "working_state_version": state.version,
            "working_state_sha256": state.sha256,
            "job_generation": generation,
            "active_job_ids": [item.id for item in active_jobs],
            "active_job_statuses": [item.status.value for item in active_jobs],
            "turn_index": state.turn_index,
            "stop_reason": state.stop_reason.value if state.stop_reason else None,
            "pending_context_snapshot_id": state.pending_context_snapshot_id,
            "pending_context_sha256": state.pending_context_sha256,
            "last_context_sha256": state.last_context_sha256,
            "last_decision_sha256": state.last_decision_sha256,
            "bridge_conversation_id": bridge_conversation_id,
            "last_operation": last_operation,
            "invocations": [_invocation_view(item) for item in ordered_invocations],
            "ambiguous_invocations": [_invocation_view(item) for item in ambiguous],
            "invocations_requiring_reconciliation": [_invocation_view(item) for item in unresolved],
            "evidence_checkpoint": {
                "observation_count": len(evidence),
                "sha256": evidence_checkpoint_sha256,
                "observations": evidence,
            },
            "opinion_checkpoint": opinion_checkpoint,
            "g2": {
                "approved": envelope is not None,
                "version": envelope.version if envelope else 0,
                "approval_decision_id": envelope.approval_decision_id if envelope else None,
                "approval_actor_id": envelope.approval_actor_id if envelope else None,
                "allowed_providers": list(envelope.allowed_providers) if envelope else [],
                "acquire_hits_allowed": envelope.acquire_hits_allowed if envelope else False,
                "disclosure": (
                    {
                        "tlp": envelope.disclosure.tlp.value if envelope.disclosure.tlp else None,
                        "do_not_submit": envelope.disclosure.do_not_submit,
                        "external_llm_allowed": envelope.disclosure.external_llm_allowed,
                    }
                    if envelope
                    else None
                ),
            },
            "budget": budget,
            "budget_reservations": reservations,
            "dependencies": {
                "available": list(available_tools),
                "unavailable": list(unavailable_tools),
            },
            "next_idempotent_action": next_action,
        }
    )


def _invocation_view(invocation: ToolInvocation) -> dict[str, Any]:
    return {
        "id": invocation.id,
        "operation_key": invocation.operation_key,
        "tool_name": invocation.tool_name,
        "status": invocation.status.value,
        "version": invocation.version,
        "response_refs": list(invocation.response_refs),
        "evidence_missing": (
            [] if invocation.response_refs else ["provider response or reconciliation evidence"]
        ),
        "decision_expected": (
            "reconcile before resume"
            if invocation.status in {ToolInvocationStatus.PENDING, ToolInvocationStatus.RUNNING}
            or (
                invocation.status is ToolInvocationStatus.NEEDS_REVIEW
                and not invocation.response_refs
            )
            else None
        ),
        "updated_at": invocation.updated_at,
    }


def _next_action(
    *,
    investigation: AnalystInvestigation,
    state: InvestigationWorkingState,
    generation: int,
    active_jobs: tuple[Job, ...],
    unresolved: tuple[ToolInvocation, ...],
    pending_bridge_key: str | None,
    bridge_conversation_id: UUID,
    envelope_version: int,
) -> dict[str, Any]:
    prefix = f"/api/investigations/{investigation.id}"
    if active_jobs:
        return {
            "code": OperatorNextAction.WAIT_FOR_WORKER.value,
            "method": "GET",
            "path": f"{prefix}/operator-status",
            "job_id": active_jobs[0].id,
            "generation": generation,
        }
    if unresolved:
        invocation = unresolved[0]
        return {
            "code": OperatorNextAction.RECONCILE_TOOL_INVOCATION.value,
            "method": "POST",
            "path": f"{prefix}/invocations/{invocation.id}/reconcile",
            "invocation_id": invocation.id,
            "expected_version": invocation.version,
            "accepted_outcomes": ["executed_with_result", "not_executed", "abandon"],
            "requires_idempotency_key": True,
        }
    if pending_bridge_key is not None and state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW:
        return {
            "code": OperatorNextAction.INSPECT_BRIDGE_TURN.value,
            "method": "GET",
            "path": f"/api/model-conversations/{bridge_conversation_id}/turns",
            "query": {"subject_id": str(investigation.subject_id)},
            "submission_key": pending_bridge_key,
            "requires_human_reconciliation": True,
        }
    if state.stop_reason is StopReason.PERMISSION_REQUIRED:
        return {
            "code": OperatorNextAction.REQUEST_G2_DECISION.value,
            "method": "POST",
            "path": f"{prefix}/envelope/approve",
            "expected_version": envelope_version,
            "requires_idempotency_key": True,
            "requires_human_decision": True,
        }
    if state.stop_reason in {StopReason.AGENT_FINISHED, StopReason.NO_NEW_INFORMATION}:
        return {"code": OperatorNextAction.NO_ACTION.value}
    return {
        "code": OperatorNextAction.RESUME_INVESTIGATION.value,
        "method": "POST",
        "path": f"{prefix}/resume",
        "expected_version": state.version,
        "next_generation": generation + 1,
        "requires_idempotency_key": True,
    }


__all__ = [
    "InvestigationOperatorErrorCode",
    "InvestigationOperatorView",
    "OperatorNextAction",
    "build_investigation_operator_view",
]

"""Read-only operator projection for durable investigation checkpoints."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.application.bridge_investigator import (
    make_investigation_conversation_id,
    make_investigation_turn_idempotency_key,
)
from cti_app.application.investigation_vt_tools import (
    VT_PASSIVE_RETRY_TRUSTED_PARAMETER,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1
from cti_app.domain.investigation import (
    BudgetDimension,
    EnvelopeAuthorityKind,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
    thaw_json,
)
from cti_app.domain.investigation_state import (
    InvestigationWorkingState,
    StopReason,
    investigation_bridge_attempt,
)
from cti_app.domain.jobs import Job, JobStatus
from cti_app.domain.production import AnalystInvestigation


class InvestigationOperatorErrorCode(StrEnum):
    INVESTIGATION_NOT_FOUND = "investigation_not_found"
    INVESTIGATION_STATE_MISSING = "investigation_not_started"
    STATUS_UNAVAILABLE = "investigation_operator_status_unavailable"
    ENVELOPE_REVOKED = "envelope_revoked"


class OperatorNextAction(StrEnum):
    WAIT_FOR_WORKER = "wait_for_worker"
    OPERATOR_ACTION_REQUIRED = "operator_action_required"
    INSPECT_BRIDGE_TURN = "inspect_bridge_turn"
    RECONCILE_MODEL_CONVERSATION_TURN = "reconcile_model_conversation_turn"
    RECONCILE_TOOL_INVOCATION = "reconcile_tool_invocation"
    RETRY_PASSIVE_READ = "retry_passive_read"
    REQUEST_G2_DECISION = "request_g2_decision"
    CREATE_NEW_INVESTIGATION = "create_new_investigation"
    RESUME_INVESTIGATION = "resume_investigation"
    ADVISOR_CHECKPOINT_DUE = "advisor_checkpoint_due"
    NO_ACTION = "no_action"


@dataclass(frozen=True, slots=True)
class InvestigationOperatorView:
    payload: dict[str, Any]


def build_investigation_operator_strategy_checkpoint(
    strategy: dict[str, Any],
    *,
    state: InvestigationWorkingState | None,
    g2_blocked: bool = False,
    unavailable_tools: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Add a checkpoint using active-strategy coverage and all persisted attempts.

    ``attempted_experiment_ids`` is historical. Coverage follows the strategy
    projection, which excludes superseded experiments, so both views use one
    interpretation of the current claim state.
    """
    claim_rows = tuple(item for item in strategy.get("claims", ()) if isinstance(item, dict))
    statuses = {str(item.get("status", "UNKNOWN")) for item in claim_rows}
    strategy_status = (
        next(iter(statuses)) if len(statuses) == 1 and "UNKNOWN" not in statuses else "UNKNOWN"
    )
    experiment_rows: dict[str, dict[str, Any]] = {}
    for claim in claim_rows:
        for experiment in claim.get("attempted_experiments", ()):
            if isinstance(experiment, dict) and isinstance(experiment.get("id"), str):
                experiment_rows[experiment["id"]] = experiment

    coverage_values = {str(item.get("coverage", "none")) for item in claim_rows}
    if not coverage_values or coverage_values == {"none"}:
        coverage = "none"
    elif coverage_values == {"complete"}:
        coverage = "complete"
    else:
        coverage = "limited"

    reasons: list[str] = []
    if state is not None and state.stop_reason is StopReason.BUDGET_EXHAUSTED:
        reasons.append("budget")
    if g2_blocked:
        reasons.append("refus_g2")
    unavailable = {item.split(":", 1)[0] for item in unavailable_tools}
    if any(
        item.get("availability") == "unavailable" or item.get("tool_name") in unavailable
        for item in experiment_rows.values()
    ):
        reasons.append("indisponible")

    seen_attempts: set[bytes] = set()
    repeated = False
    for item in sorted(
        experiment_rows.values(), key=lambda row: (row.get("version", 0), row["id"])
    ):
        attempt_key = canonical_json_bytes(
            {
                "hypothesis_id": item.get("hypothesis_id"),
                "tool_name": item.get("tool_name"),
                "attempt_identity_sha256": item.get("attempt_identity_sha256"),
            }
        )
        repeated = repeated or attempt_key in seen_attempts
        seen_attempts.add(attempt_key)
    if repeated:
        reasons.append("deja_testee")
    if not reasons:
        reasons.append("inconnue")

    known_blockers = set(reasons) - {"inconnue"}
    next_action = {
        "code": (
            "resolve_blocker"
            if known_blockers
            else "review_existing_experiments"
            if experiment_rows
            else "select_discriminating_experiment"
        ),
        "status": "blocked"
        if known_blockers
        else "ready"
        if strategy_status != "UNKNOWN"
        else "unknown",
        "reason": reasons[0],
    }
    return {
        "checkpoint": {
            "status": strategy_status,
            "coverage": coverage,
            "attempted_experiment_ids": sorted(experiment_rows),
            "blocking_reasons": reasons,
        },
        "next_action": next_action,
    }


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
    advisor_guidance: dict[str, Any] | None = None,
    g2_revoked: bool = False,
    g2_reapproval_supported: bool = False,
    policy_authority: dict[str, Any] | None = None,
    pending_bridge_turn_id: UUID | None = None,
    pending_bridge_turn_status: str | None = None,
    pending_bridge_turn_error_code: str | None = None,
    pending_bridge_turn_key: str | None = None,
    provider_response_ids: Mapping[UUID, str] | None = None,
) -> InvestigationOperatorView:
    (
        projected_available_tools,
        projected_unavailable_tools,
        retryable_vt_tool_versions,
    ) = _project_operator_tool_inventory(
        available_tools, unavailable_tools, envelope=envelope, g2_revoked=g2_revoked
    )
    guidance = advisor_guidance or {
        "checkpoint_due": False,
        "reason": None,
        "suggested_consultation_type": None,
        "last_advice": None,
        "reminder": "Bridge: sortie non fiable; valider chaque suggestion par des mesures.",
        "advisor_turns_used": 0,
        "advisor_turn_limit": None,
        "advisor_turns_remaining": None,
        "truncated": False,
    }
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
        and (item.status is ToolInvocationStatus.NEEDS_REVIEW or not item.response_refs)
    )
    ambiguous = tuple(
        item
        for item in unresolved
        if item.status in {ToolInvocationStatus.RUNNING, ToolInvocationStatus.NEEDS_REVIEW}
    )
    invocation_ids_by_operation_key = {item.operation_key: item.id for item in ordered_invocations}
    latest_invocation = ordered_invocations[-1] if ordered_invocations else None
    pending_bridge_key = None
    bridge_conversation_id = make_investigation_conversation_id(investigation.id)
    if state.pending_context_sha256 is not None:
        attempt = investigation_bridge_attempt(state.recent_actions, state.turn_index)
        pending_bridge_key = make_investigation_turn_idempotency_key(
            investigation_id=investigation.id,
            turn_index=state.turn_index,
            context_sha256=state.pending_context_sha256,
            attempt=attempt,
        )
    pre_submission_retry_exhausted = any(
        item.startswith(f"bridge_turn_pre_submission_retry_exhausted:{state.turn_index}:")
        for item in state.recent_actions
    )

    if pending_bridge_key is not None:
        last_operation: dict[str, Any] | None = {
            "kind": "bridge_turn",
            "id": pending_bridge_turn_key or pending_bridge_key,
            "turn_id": pending_bridge_turn_id,
            "conversation_id": bridge_conversation_id,
            "conversation_id_scope": "local_autowork_record_only",
            "external_target": "temporary_chat_per_turn",
            "turn_index": state.turn_index,
            "context_snapshot_id": state.pending_context_snapshot_id,
            "context_sha256": state.pending_context_sha256,
            "status": (
                "pre_submission_retry_exhausted"
                if pre_submission_retry_exhausted
                else pending_bridge_turn_status
            )
            or (
                "needs_review"
                if state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
                else "pending_or_running"
            ),
            "error_code": (
                "bridge_pre_submission_retry_exhausted"
                if pre_submission_retry_exhausted
                else pending_bridge_turn_error_code
            ),
            "evidence_missing": (
                []
                if pre_submission_retry_exhausted or pending_bridge_turn_status == "succeeded"
                else ["persisted Bridge turn result or provider outcome"]
            ),
            "decision_expected": (
                "resume to submit the next distinct attempt after confirmed pre-submission failures"
                if pre_submission_retry_exhausted
                else "consume adopted output after resume"
                if pending_bridge_turn_status == "succeeded"
                else "reconcile orphaned Bridge turn before a distinct retry"
                if pending_bridge_turn_error_code == "orphaned_running_turn"
                else "reconcile interrupted Bridge turn before resume"
                if pending_bridge_turn_error_code == "operator_interrupted"
                else "reconcile the exact provider result after legacy abandonment"
                if pending_bridge_turn_error_code == "operator_abandoned"
                else "reconcile the exact persisted turn before resume"
            ),
        }
    elif latest_invocation is not None:
        last_operation = _invocation_view(
            latest_invocation,
            investigation_id=investigation.id,
            working_state_version=state.version,
            available_tools=projected_available_tools,
            retryable_vt_tool_versions=retryable_vt_tool_versions,
        )
    else:
        last_operation = None

    context_limit_action = next(
        (
            item
            for item in reversed(state.recent_actions)
            if item.startswith("investigation_context_too_large:")
        ),
        None,
    )
    last_error = (
        {
            "code": "investigation_context_too_large",
            "details": context_limit_action.removeprefix("investigation_context_too_large:"),
        }
        if context_limit_action is not None
        else None
    )

    dimensions = envelope.budget if envelope is not None else None
    fallback_limits = {
        BudgetDimension.BRIDGE_TURNS: None,
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
                "invocation_id": invocation_ids_by_operation_key.get(item.operation_key),
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
        pending_bridge_turn_id=pending_bridge_turn_id,
        pending_bridge_turn_status=pending_bridge_turn_status,
        pending_bridge_turn_error_code=pending_bridge_turn_error_code,
        pending_bridge_turn_key=pending_bridge_turn_key,
        envelope_version=envelope.version if envelope else 0,
        g2_revoked=g2_revoked,
        g2_reapproval_supported=g2_reapproval_supported,
        advisor_guidance=guidance,
        available_tools=projected_available_tools,
        retryable_vt_tool_versions=retryable_vt_tool_versions,
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
            "execution_mode": state.execution_mode.value,
            "investigation_version": investigation.version,
            "working_state_version": state.version,
            "working_state_sha256": state.sha256,
            "deduplicated_findings": state.deduplicated_findings,
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
            "bridge_conversation_id_scope": "local_autowork_record_only",
            "bridge_external_target": {
                "mode": (
                    "disabled"
                    if state.execution_mode is InvestigationExecutionMode.OPERATOR_LED
                    else "stateless"
                ),
                "lifecycle": (
                    "no_automatic_turns"
                    if state.execution_mode is InvestigationExecutionMode.OPERATOR_LED
                    else "temporary_chat_per_turn"
                ),
            },
            "last_error": last_error,
            "last_operation": last_operation,
            "invocations": [
                _invocation_view(
                    item,
                    investigation_id=investigation.id,
                    provider_response_id=(provider_response_ids or {}).get(item.id),
                    working_state_version=state.version,
                    available_tools=projected_available_tools,
                    retryable_vt_tool_versions=retryable_vt_tool_versions,
                )
                for item in ordered_invocations
            ],
            "ambiguous_invocations": [
                _invocation_view(
                    item,
                    investigation_id=investigation.id,
                    provider_response_id=(provider_response_ids or {}).get(item.id),
                    working_state_version=state.version,
                    available_tools=projected_available_tools,
                    retryable_vt_tool_versions=retryable_vt_tool_versions,
                )
                for item in ambiguous
            ],
            "invocations_requiring_reconciliation": [
                _invocation_view(
                    item,
                    investigation_id=investigation.id,
                    provider_response_id=(provider_response_ids or {}).get(item.id),
                    working_state_version=state.version,
                    available_tools=projected_available_tools,
                    retryable_vt_tool_versions=retryable_vt_tool_versions,
                )
                for item in unresolved
            ],
            "evidence_checkpoint": {
                "observation_count": len(evidence),
                "sha256": evidence_checkpoint_sha256,
                "observations": evidence,
            },
            "opinion_checkpoint": opinion_checkpoint,
            "g2": {
                "approved": envelope is not None,
                "revoked": g2_revoked,
                "reapproval_supported": g2_reapproval_supported,
                "version": envelope.version if envelope else 0,
                "approval_decision_id": (
                    envelope.approval_decision_id
                    if (
                        envelope
                        and envelope.authority_kind is not EnvelopeAuthorityKind.OSINT_POLICY
                    )
                    else None
                ),
                "approval_actor_id": (
                    envelope.approval_actor_id
                    if (
                        envelope
                        and envelope.authority_kind is not EnvelopeAuthorityKind.OSINT_POLICY
                    )
                    else None
                ),
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
            "policy_authority": policy_authority,
            "budget": budget,
            "budget_reservations": reservations,
            "dependencies": {
                "available": list(projected_available_tools),
                "unavailable": list(projected_unavailable_tools),
            },
            "tool_inventory": {
                "available": list(projected_available_tools),
                "unavailable": list(projected_unavailable_tools),
            },
            "advisor_guidance": guidance,
            "next_idempotent_action": next_action,
        }
    )


_PASSIVE_VT_RETRY_ARGUMENTS = {
    "vt.file_report": frozenset({"file_hash"}),
    "vt.object_report": frozenset({"object_type", "object_id"}),
    "vt.relationships": frozenset({"object_type", "object_id", "relation"}),
    "vt.intelligence_search": frozenset({"query", "justification", "evidence_refs"}),
    "vt.behaviour": frozenset({"file_hash"}),
    "vt.behaviour_pcap": frozenset({"file_hash", "sandbox_id"}),
}
_VT_RETRY_MAX_RESERVATIONS = {
    "vt.file_report": {BudgetDimension.VT_UNITS: 1},
    "vt.object_report": {BudgetDimension.VT_UNITS: 2},
    # The operator projection receives names, not registered cost models;
    # relationship/search/behaviour adapters can reserve up to 10 pages.
    "vt.relationships": {BudgetDimension.VT_UNITS: 10},
    "vt.intelligence_search": {BudgetDimension.VT_UNITS: 10},
    "vt.behaviour": {BudgetDimension.VT_UNITS: 10},
    "vt.behaviour_pcap": {BudgetDimension.VT_UNITS: 1},
    "vt.download_sample": {
        BudgetDimension.VT_UNITS: 1,
        BudgetDimension.DOWNLOADS: 1,
    },
}


def _project_operator_tool_inventory(
    available_tools: tuple[str, ...],
    unavailable_tools: tuple[str, ...],
    *,
    envelope: InvestigationEnvelope | None,
    g2_revoked: bool,
) -> tuple[tuple[str, ...], tuple[str, ...], frozenset[tuple[str, str]]]:
    """Hide externally gated tools whose current blocker is visible in status."""
    diagnostics: set[str] = set()
    retryable_vt_tool_versions: set[tuple[str, str]] = set()
    for item in unavailable_tools:
        name, separator, reason = item.partition(":")
        if separator and reason == "passive_retry_ready":
            tool_name, version_separator, schema_version = name.rpartition("@")
            if version_separator:
                retryable_vt_tool_versions.add((tool_name, schema_version))
                continue
        diagnostics.add(item)
    bridge_disclosure_denied = any(
        item == "bridge.advisor:external_llm_disclosure_denied" for item in unavailable_tools
    )
    vt_reason: str | None = None
    if g2_revoked:
        vt_reason = "envelope_revoked"
    elif (
        envelope is None or bridge_disclosure_denied or not envelope.disclosure.external_llm_allowed
    ):
        vt_reason = "external_disclosure_denied"
    elif "vt" not in envelope.allowed_providers:
        vt_reason = "provider_not_authorized"
    elif envelope.disclosure.tlp is not None and envelope.disclosure.tlp.value == "red":
        vt_reason = "external_disclosure_denied"

    visible: list[str] = []
    for name in available_tools:
        reason = vt_reason if name.startswith("vt.") else None
        if reason is None and name in _VT_RETRY_MAX_RESERVATIONS and envelope is not None:
            for dimension, amount in _VT_RETRY_MAX_RESERVATIONS[name].items():
                remaining = envelope.budget.remaining.get(dimension)
                if remaining is not None and remaining < amount:
                    reason = "budget_exhausted"
                    break
        if (
            name == "vt.download_sample"
            and envelope is not None
            and not envelope.acquire_hits_allowed
        ):
            reason = "acquisition_not_authorized"
        if reason is None:
            visible.append(name)
        else:
            diagnostics.add(f"{name}:{reason}")
    visible_names = set(visible)
    retryable_vt_tool_versions = {
        key for key in retryable_vt_tool_versions if key[0] in visible_names
    }
    return (
        tuple(visible),
        tuple(sorted(diagnostics)),
        frozenset(retryable_vt_tool_versions),
    )


def _passive_retry_action(
    invocation: ToolInvocation,
    *,
    investigation_id: UUID,
    working_state_version: int,
    available_tools: tuple[str, ...],
    retryable_vt_tool_versions: frozenset[tuple[str, str]],
) -> dict[str, Any] | None:
    fields = _PASSIVE_VT_RETRY_ARGUMENTS.get(invocation.tool_name)
    if (
        invocation.status is not ToolInvocationStatus.NEEDS_REVIEW
        or fields is None
        or invocation.tool_name not in available_tools
    ):
        return None
    retryable_schema_versions = sorted(
        schema_version
        for tool_name, schema_version in retryable_vt_tool_versions
        if tool_name == invocation.tool_name
    )
    if not retryable_schema_versions:
        return None
    raw_arguments = thaw_json(invocation.normalized_parameters)
    if (
        not isinstance(raw_arguments, Mapping)
        or raw_arguments.get(VT_PASSIVE_RETRY_TRUSTED_PARAMETER) is not True
    ):
        return None
    arguments = {key: value for key, value in raw_arguments.items() if key in fields}
    required = {
        "vt.file_report": {"file_hash"},
        "vt.object_report": {"object_type", "object_id"},
        "vt.relationships": {"object_type", "object_id", "relation"},
        "vt.intelligence_search": {"query", "justification", "evidence_refs"},
        "vt.behaviour": {"file_hash"},
        "vt.behaviour_pcap": {"file_hash", "sandbox_id"},
    }[invocation.tool_name]
    if not required.issubset(arguments):
        return None
    if invocation.tool_name == "vt.intelligence_search":
        refs = arguments["evidence_refs"]
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
            return None
        try:
            tuple(UUID(ref) for ref in refs)
        except ValueError:
            return None
    retry_schema_version = (
        invocation.schema_version
        if invocation.schema_version in retryable_schema_versions
        else retryable_schema_versions[0]
    )
    return {
        "method": "POST",
        "path": f"/api/investigations/{investigation_id}/operator/tools/{invocation.tool_name}",
        "tool_name": invocation.tool_name,
        "schema_version": retry_schema_version,
        "arguments": arguments,
        "purpose": "Retry an idempotent passive VT read as a new occurrence.",
        "expected_version": working_state_version,
        "idempotency_key": f"operator-passive-retry:{invocation.id}:{invocation.version}",
        "requires_new_occurrence": True,
        "resubmission_allowed": True,
    }


def _invocation_view(
    invocation: ToolInvocation,
    *,
    investigation_id: UUID | None = None,
    provider_response_id: str | None = None,
    working_state_version: int | None = None,
    available_tools: tuple[str, ...] = (),
    retryable_vt_tool_versions: frozenset[tuple[str, str]] = frozenset(),
) -> dict[str, Any]:
    needs_reconciliation = invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    reconciliation_action = (
        _reconciliation_action(invocation, investigation_id=investigation_id)
        if investigation_id is not None and needs_reconciliation
        else None
    )
    retry_action = (
        _passive_retry_action(
            invocation,
            investigation_id=investigation_id,
            working_state_version=working_state_version,
            available_tools=available_tools,
            retryable_vt_tool_versions=retryable_vt_tool_versions,
        )
        if investigation_id is not None and working_state_version is not None
        else None
    )
    return {
        "id": invocation.id,
        "operation_key": invocation.operation_key,
        "tool_name": invocation.tool_name,
        "status": invocation.status.value,
        "version": invocation.version,
        "response_refs": list(invocation.response_refs),
        "provider_response_id": provider_response_id,
        "resubmission_allowed": retry_action is not None,
        "evidence_missing": (
            ["provider outcome reconciliation"]
            if needs_reconciliation
            else []
            if invocation.response_refs
            else ["provider response or reconciliation evidence"]
        ),
        "decision_expected": (
            "retry passive read as a new occurrence"
            if retry_action is not None
            else "reconcile before resume"
            if invocation.status in {ToolInvocationStatus.PENDING, ToolInvocationStatus.RUNNING}
            or needs_reconciliation
            else None
        ),
        "reconciliation_action": reconciliation_action,
        "retry_action": retry_action,
        "updated_at": invocation.updated_at,
    }


def _reconciliation_action(
    invocation: ToolInvocation,
    *,
    investigation_id: UUID,
) -> dict[str, Any]:
    prefix = f"/api/investigations/{investigation_id}"
    if invocation.tool_name == "advisor.consult":
        return {
            "method": "POST",
            "path": f"{prefix}/operator/invocations/{invocation.id}/reconcile",
            "expected_version": invocation.version,
            "resubmission_allowed": False,
        }
    return {
        "method": "POST",
        "path": f"{prefix}/invocations/{invocation.id}/reconcile",
        "expected_version": invocation.version,
        "accepted_outcomes": ["executed_with_result", "not_executed", "abandon"],
        "reason_required": True,
        "response_refs_allowed_for": "executed_with_result",
        "idempotency_key_header": "Idempotency-Key",
        "requires_verified_human_operator": True,
        "resubmission_allowed": False,
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
    pending_bridge_turn_id: UUID | None,
    pending_bridge_turn_status: str | None,
    pending_bridge_turn_error_code: str | None,
    pending_bridge_turn_key: str | None,
    envelope_version: int,
    g2_revoked: bool,
    g2_reapproval_supported: bool,
    advisor_guidance: dict[str, Any],
    available_tools: tuple[str, ...],
    retryable_vt_tool_versions: frozenset[tuple[str, str]],
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
        if invocation.status is ToolInvocationStatus.NEEDS_REVIEW:
            retry_action = _passive_retry_action(
                invocation,
                investigation_id=investigation.id,
                working_state_version=state.version,
                available_tools=available_tools,
                retryable_vt_tool_versions=retryable_vt_tool_versions,
            )
            if retry_action is not None:
                return {
                    "code": OperatorNextAction.RETRY_PASSIVE_READ.value,
                    "invocation_id": invocation.id,
                    **retry_action,
                }
            return {
                "code": OperatorNextAction.RECONCILE_TOOL_INVOCATION.value,
                "invocation_id": invocation.id,
                **_reconciliation_action(invocation, investigation_id=investigation.id),
            }
        return {
            "code": OperatorNextAction.RECONCILE_TOOL_INVOCATION.value,
            "method": "POST",
            "path": f"{prefix}/invocations/{invocation.id}/reconcile",
            "invocation_id": invocation.id,
            "expected_version": invocation.version,
            "accepted_outcomes": ["executed_with_result", "not_executed", "abandon"],
            "requires_idempotency_key": True,
        }
    if state.execution_mode is InvestigationExecutionMode.OPERATOR_LED:
        suggested = advisor_guidance.get("suggested_consultation_type")
        if advisor_guidance.get("checkpoint_due") and isinstance(suggested, str):
            return {
                "code": OperatorNextAction.ADVISOR_CHECKPOINT_DUE.value,
                "reason": advisor_guidance.get("reason"),
                "blocking": False,
                "tool_name": "advisor.consult",
                "method": "POST",
                "path": f"{prefix}/operator/tools/advisor.consult",
                "arguments": {"consultation_type": suggested},
                "purpose": "Bounded advisor checkpoint; validate suggestions before acting.",
                "expected_version": state.version,
                "requires_idempotency_key": True,
            }
        return {
            "code": OperatorNextAction.OPERATOR_ACTION_REQUIRED.value,
            "method": "GET",
            "path": f"{prefix}/operator/context",
            "expected_version": state.version,
        }
    retry_exhausted_action = any(
        item.startswith(f"bridge_turn_pre_submission_retry_exhausted:{state.turn_index}:")
        for item in state.recent_actions
    )
    if (
        pending_bridge_key is not None
        and state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
        and retry_exhausted_action
    ):
        return {
            "code": OperatorNextAction.RESUME_INVESTIGATION.value,
            "reason": "bridge_pre_submission_retry_exhausted",
            "method": "POST",
            "path": f"{prefix}/resume",
            "expected_version": state.version,
            "next_generation": generation + 1,
            "requires_idempotency_key": True,
        }
    if (
        pending_bridge_key is not None
        and pending_bridge_turn_id is not None
        and (
            (
                pending_bridge_turn_status == "needs_review"
                and pending_bridge_turn_error_code
                in {"operator_interrupted", "orphaned_running_turn"}
            )
            or (
                pending_bridge_turn_status == "failed"
                and pending_bridge_turn_error_code == "operator_abandoned"
            )
        )
    ):
        turn_prefix = (
            f"/api/model-conversations/{bridge_conversation_id}/turns/"
            f"{pending_bridge_turn_id}/reconciliation"
        )
        return {
            "code": OperatorNextAction.RECONCILE_MODEL_CONVERSATION_TURN.value,
            "reason": "legacy_abandonment"
            if pending_bridge_turn_error_code == "operator_abandoned"
            else "orphaned_running_turn"
            if pending_bridge_turn_error_code == "orphaned_running_turn"
            else "operator_interrupted",
            "method": "POST",
            "path": f"{turn_prefix}/visible/preview",
            "preview_path": f"{turn_prefix}/visible/preview",
            "adopt_path": f"{turn_prefix}/visible/adopt",
            "query": {"subject_id": str(investigation.subject_id)},
            "submission_key": pending_bridge_turn_key or pending_bridge_key,
            "requires_human_reconciliation": True,
            "adoption_requires_expected_sha256": True,
            "resubmission_allowed": False,
        }
    if pending_bridge_key is not None and state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW:
        if pending_bridge_turn_status == "succeeded" or (
            pending_bridge_turn_status == "failed"
            and pending_bridge_turn_error_code == "bridge_pre_submission_failure"
        ):
            return {
                "code": OperatorNextAction.RESUME_INVESTIGATION.value,
                "method": "POST",
                "path": f"{prefix}/resume",
                "expected_version": state.version,
                "next_generation": generation + 1,
                "requires_idempotency_key": True,
                "adopted_turn_id": (
                    pending_bridge_turn_id if pending_bridge_turn_status == "succeeded" else None
                ),
            }
        if pending_bridge_turn_id is not None and pending_bridge_turn_status == "needs_review":
            turn_prefix = (
                f"/api/model-conversations/{bridge_conversation_id}/turns/"
                f"{pending_bridge_turn_id}/reconciliation"
            )
            return {
                "code": OperatorNextAction.RECONCILE_MODEL_CONVERSATION_TURN.value,
                "method": "POST",
                "path": f"{turn_prefix}/visible/preview",
                "preview_path": f"{turn_prefix}/visible/preview",
                "adopt_path": f"{turn_prefix}/visible/adopt",
                "query": {"subject_id": str(investigation.subject_id)},
                "submission_key": pending_bridge_turn_key or pending_bridge_key,
                "requires_human_reconciliation": True,
                "adoption_requires_expected_sha256": True,
                "adoption_is_not_g2_g3_approval": True,
                "resubmission_allowed": False,
            }
        return {
            "code": OperatorNextAction.INSPECT_BRIDGE_TURN.value,
            "method": "GET",
            "path": f"/api/model-conversations/{bridge_conversation_id}/turns",
            "query": {"subject_id": str(investigation.subject_id)},
            "submission_key": pending_bridge_key,
            "requires_human_reconciliation": True,
        }
    if state.stop_reason is StopReason.PERMISSION_REQUIRED:
        if g2_revoked and g2_reapproval_supported:
            return {
                "code": OperatorNextAction.REQUEST_G2_DECISION.value,
                "method": "POST",
                "path": f"{prefix}/envelope/extend",
                "expected_version": envelope_version,
                "requires_idempotency_key": True,
                "requires_human_decision": True,
                "authorization_ceiling_enforced": True,
            }
        if g2_revoked:
            return {
                "code": OperatorNextAction.CREATE_NEW_INVESTIGATION.value,
                "method": "POST",
                "path": f"/api/subjects/{investigation.subject_id}/investigations",
                "request_fields": [
                    "production_run_id",
                    "synthesis_artifact_id",
                    "synthesis_sha256",
                ],
                "requires_new_verified_synthesis": True,
            }
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

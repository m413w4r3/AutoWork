"""Deterministic, redacted audit projection for one investigation snapshot.

The export is a read-only projection of versioned application records. It is not
a source of CTI evidence, does not reconstruct missing transport history, and
never serializes arbitrary source text or parameters.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetReservationStatus,
    DetectionCandidateStatus,
    EvidenceKind,
    FindingAssessment,
    FindingKind,
    InvestigationExecutionMode,
    NoveltyClass,
    QualificationActorKind,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_experiment import (
    ExperimentAvailability,
    ExperimentVerdict,
    ObservationCoverage,
    ObservationOutcome,
)
from cti_app.domain.investigation_state import HypothesisStatus, StopReason
from cti_app.domain.model_conversations import (
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
)
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelRunStatus,
    ModelSubmissionState,
    ModelTransport,
)

AUDIT_EVENT_SCHEMA = "autowork.investigation-audit-event.v1"
AUDIT_MANIFEST_SCHEMA = "autowork.investigation-audit-manifest.v1"
AUDIT_INDEX_SCHEMA = "autowork.investigation-audit-index.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SAFE_TOOL_NAMES = frozenset(
    {
        "advisor.consult",
        "features.common",
        "features.explain",
        "local.run_command",
        "sample.analyze",
        "sample.get_feature_pack",
        "samples.compare",
        "shodan.count",
        "shodan.host",
        "signatures.candidates",
        "vt.behaviour",
        "vt.file_report",
        "vt.intelligence_search",
        "vt.lookup",
        "vt.object_report",
        "vt.relationships",
    }
)
_SAFE_OBJECT_TYPES = frozenset({"advisor_call", "virustotal", "sample", "other"})
_SAFE_CONSULTATION_TYPES = frozenset(
    {
        "draft_text",
        "figure_plan",
        "pivot_strategy",
        "plan_investigation",
        "review_text",
        "suricata_draft",
        "yara_draft",
    }
)
_SOURCE_VERSION_ALLOWLIST = {
    "investigation_state": "investigation-state.v1",
    "investigation_experiment": "investigation-experiment.v1",
    "analyst_opinion": "analyst-opinion.v1",
    "model_conversation": "model-conversation.repository.v1",
    "audit_projection": AUDIT_EVENT_SCHEMA,
}


@dataclass(frozen=True, slots=True)
class AuditSourceSnapshot:
    """Canonical repository values captured in one read-only DB snapshot."""

    investigation_id: str
    invocations: Sequence[Any] = ()
    observations: Sequence[Any] = ()
    findings: Sequence[Any] = ()
    experiments: Sequence[Any] = ()
    hypotheses_state: Any | None = None
    envelope: Any | None = None
    budget_snapshot: Any | None = None
    opinions: Sequence[Any] = ()
    detection_candidates: Sequence[Any] = ()
    sample_associations: Sequence[Any] = ()
    pivot_conversation_id: str | None = None
    conversation: Any | None = None
    conversation_turns: Sequence[Any] = ()
    model_runs: Sequence[Any] = ()
    source_versions: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditBundle:
    events: bytes
    trace: bytes
    index: bytes
    manifest: bytes
    event_count: int
    resumed_from_events: bool = False


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):
        value = value.value
    result = str(value)
    return result if result else None


def _enum_token(value: Any, enum_type: type[StrEnum]) -> str | None:
    result = _text(value)
    if result is None:
        return None
    return result if result in {member.value for member in enum_type} else None


def _safe_tool_name(value: Any) -> str:
    result = _text(value)
    return result if result in _SAFE_TOOL_NAMES else "UNKNOWN"


def _safe_object_type(value: Any) -> str:
    result = _text(value)
    return result if result in _SAFE_OBJECT_TYPES else "OTHER"


def _safe_consultation_type(value: Any) -> str:
    result = _text(value)
    return result if result in _SAFE_CONSULTATION_TYPES else "UNKNOWN"


def _opaque_id(value: Any) -> str | None:
    result = _text(value)
    if not result:
        return None
    try:
        return str(UUID(result))
    except ValueError:
        return f"sha256:{_sha256_value(result)}"


def _uuid_value(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return "UNKNOWN"


def _required_uuid(value: Any, field_name: str) -> str:
    result = _uuid_value(value)
    if result == "UNKNOWN":
        raise ValueError(f"{field_name} must be a UUID")
    return result


def _sha_value(value: Any) -> str:
    result = _text(value)
    return result if result and _SHA256.fullmatch(result) else "UNKNOWN"


def _idempotency_fingerprint(value: Any) -> dict[str, str]:
    result = _text(value)
    if result is None:
        return {"value": "UNKNOWN", "representation": "missing"}
    return {"value": _sha256_value(result) or "UNKNOWN", "representation": "derived_sha256"}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str | None:
    if value is None:
        return None
    return _sha256_bytes(
        value.encode("utf-8") if isinstance(value, str) else _canonical_bytes(value)
    )


def _unknown(reason: str) -> dict[str, str]:
    return {"value": "UNKNOWN", "reason": reason}


def _boolean_or_unknown(value: Any) -> bool | str:
    return value if isinstance(value, bool) else "UNKNOWN"


def _material_stage(reason: str) -> dict[str, Any]:
    return {
        field: _unknown(reason)
        for field in (
            "available",
            "required",
            "selected",
            "rendered",
            "sent",
            "omitted",
            "sha256",
        )
    }


def _stable_id(event_without_id: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_bytes(event_without_id))


def _correlation(
    investigation_id: str,
    *,
    invocation_id: Any = None,
    consultation_id: Any = None,
    model_run_id: Any = None,
    turn_id: Any = None,
    bridge_run_id: Any = None,
) -> dict[str, str]:
    values = {
        "investigation_id": _required_uuid(investigation_id, "investigation_id"),
        "invocation_id": _opaque_id(invocation_id),
        "consultation_id": _opaque_id(consultation_id),
        "model_run_id": _opaque_id(model_run_id),
        "turn_id": _opaque_id(turn_id),
        "bridge_run_id": _opaque_id(bridge_run_id),
    }
    return {key: value for key, value in values.items() if value is not None}


def _event(
    kind: str,
    record_id: str,
    occurred_at: Any,
    correlation: Mapping[str, str],
    details: Mapping[str, Any],
    *,
    version: Any = None,
) -> dict[str, Any]:
    body = {
        "schema": AUDIT_EVENT_SCHEMA,
        "type": kind,
        "occurred_at": _iso(occurred_at),
        "record": {
            "id": record_id,
            "version": _json_value(version) if version is not None else None,
        },
        "correlation": dict(sorted(correlation.items())),
        "details": _json_value(details),
    }
    return {"event_id": _stable_id(body), **body}


def _event_sort_key(item: Mapping[str, Any]) -> tuple[bool, str, str, str, str]:
    return (
        item["occurred_at"] is None,
        item["occurred_at"] or "",
        item["type"],
        item["record"]["id"],
        item["event_id"],
    )


def _budget_amounts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, int] = {}
    for key, amount in value.items():
        name = _enum_token(key, BudgetDimension)
        if name and isinstance(amount, int) and not isinstance(amount, bool):
            output[name] = amount
    return dict(sorted(output.items()))


def _invocation_event(
    investigation_id: str,
    invocation: Any,
    reservations: Mapping[str, Any],
    attachments: Mapping[str, list[dict[str, str]]],
) -> dict[str, Any]:
    invocation_id = _uuid_value(_get(invocation, "id"))
    operation_key = _text(_get(invocation, "operation_key"))
    status = _text(_get(invocation, "status"))
    reservation = reservations.get(operation_key or "")
    reservation_status = _text(_get(reservation, "status")) if reservation else None
    reserved = _budget_amounts(_get(invocation, "reserved_budget", {}))
    committed = _budget_amounts(_get(invocation, "consumed_budget", {}))
    response_refs = sorted(
        {_uuid_value(value) for value in (_get(invocation, "response_refs", ()) or ())}
    )
    correlation = _correlation(investigation_id, invocation_id=invocation_id)
    if operation_key:
        correlation["operation_key_sha256"] = _sha256_value(operation_key) or "UNKNOWN"
    return _event(
        "tool_invocation.snapshot",
        invocation_id,
        _get(invocation, "created_at"),
        correlation,
        {
            "tool": _safe_tool_name(_get(invocation, "tool_name")),
            "tool_name_sha256": _sha256_value(_get(invocation, "tool_name")),
            "schema_version_sha256": _sha256_value(_get(invocation, "schema_version")),
            "purpose_sha256": _sha256_value(_get(invocation, "purpose")),
            "purpose_included": False,
            "status_at_snapshot": _enum_token(status, ToolInvocationStatus) or "UNKNOWN",
            "admission": {
                "requested": _unknown("requested budget amount is not stored on the invocation"),
                "reserved": {"amounts": reserved, "source": "tool_invocations.reserved_budget"},
                "committed": {"amounts": committed, "source": "tool_invocations.consumed_budget"},
                "released": {
                    "status_at_snapshot": reservation_status or "UNKNOWN",
                    "reason": None if reservation_status else "no matching budget reservation row",
                },
                "transition_history": _unknown(
                    "only current invocation and reservation state is stored"
                ),
            },
            "submission": _unknown(
                "no direct persisted transport state is linked to this invocation"
            ),
            "reparse": _unknown("no parser or reparse record is linked to this invocation"),
            "parser_version": _unknown("no parser version is linked to this invocation"),
            "attached_opinion_refs": attachments.get(invocation_id, []),
            "attachment": (
                {
                    "state": "attached",
                    "opinion_refs": attachments[invocation_id],
                    "source": "explicit BridgeDraftReferenceV1 or BridgeReviewReferenceV1",
                }
                if attachments.get(invocation_id)
                else {
                    "state": "no_direct_opinion_attachment_found",
                    "reason": "no matching Bridge reference in the loaded opinion versions",
                }
            ),
            "latency_ms": _unknown("invocation duration is not stored"),
            "cost_estimate": _unknown(
                "monetary estimate is not stored; budget units are reported separately"
            ),
            "materials": {
                "dossier": _material_stage("no durable dossier stage record is linked"),
                "rendered_prompt": _material_stage("no durable rendered-prompt record is linked"),
                "http_payload": _material_stage("no observed HTTP payload record is linked"),
                "raw_response": _material_stage("no raw response record is linked"),
                "normalized_output": _material_stage("no normalized output record is linked"),
                "redacted_copy": _material_stage("no redacted-copy record is linked"),
            },
            "response_refs": response_refs,
            "bridge_run_id": _unknown("no Bridge-run identity is stored on ToolInvocation"),
            "bridge_prompt_observation": {
                "prompt_as_file": _unknown(
                    "no persisted Bridge prompt-as-file observation is linked"
                ),
                "prompt_bytes": _unknown("no persisted Bridge prompt-bytes observation is linked"),
            },
            "direct_correlation_gaps": {
                "consultation_id": _unknown("no consultation record is stored"),
                "model_run_id": _unknown("no ToolInvocation-to-ModelRun foreign key is stored"),
                "turn_id": _unknown("no ToolInvocation-to-turn foreign key is stored"),
            },
        },
        version=_get(invocation, "version"),
    )


def _reservation_map(snapshot: Any) -> dict[str, Any]:
    budget = _get(snapshot, "budget") if snapshot is not None else None
    reservations = _get(budget, "reservations", ()) if budget is not None else ()
    result: dict[str, Any] = {}
    for item in reservations or ():
        key = _text(_get(item, "operation_key"))
        if key:
            result[key] = item
    return result


def _state_event(investigation_id: str, state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    agenda = _get(state, "agenda")
    agenda_items = _get(agenda, "items", ()) or ()
    questions = [
        {
            "rank": _get(item, "rank"),
            "question_sha256": _sha256_value(_get(item, "question")),
            "potential_sha256": _sha256_value(_get(item, "potential")),
        }
        for item in agenda_items
    ]
    hypotheses = []
    for item in _get(state, "hypotheses", ()) or ():
        hypotheses.append(
            {
                "id": _opaque_id(_get(item, "id")) or "UNKNOWN",
                "status": _enum_token(_get(item, "status"), HypothesisStatus) or "UNKNOWN",
                "statement_sha256": _sha256_value(_get(item, "statement")),
                "evidence_refs": sorted(
                    _uuid_value(ref) for ref in (_get(item, "evidence_refs", ()) or ())
                ),
                "finding_refs": sorted(
                    _uuid_value(ref) for ref in (_get(item, "finding_refs", ()) or ())
                ),
                "decision_reason": _unknown("hypothesis transition reason is not stored"),
            }
        )
    return _event(
        "operator_state.snapshot",
        investigation_id,
        None,
        _correlation(investigation_id),
        {
            "version": _get(state, "version"),
            "turn_index": _get(state, "turn_index"),
            "execution_mode": (
                _enum_token(_get(state, "execution_mode"), InvestigationExecutionMode) or "UNKNOWN"
            ),
            "stop_reason": _enum_token(_get(state, "stop_reason"), StopReason),
            "objective": _unknown(
                "no separate objective field is stored in the investigation state"
            ),
            "questions": questions,
            "hypotheses": hypotheses,
            "recent_action_count": len(_get(state, "recent_actions", ()) or ()),
            "recent_actions_sha256": _sha256_value(list(_get(state, "recent_actions", ()) or ())),
            "parent_child_sessions": _unknown(
                "session parent/child identities are not stored in this investigation state"
            ),
        },
        version=_get(state, "version"),
    )


def _observation_event(investigation_id: str, observation: Any) -> dict[str, Any]:
    object_type = _safe_object_type(_get(observation, "object_type"))
    invocation_ref = (
        _uuid_value(_get(observation, "invocation_ref"))
        if _get(observation, "invocation_ref") is not None
        else None
    )
    return _event(
        "evidence_observation.recorded",
        _uuid_value(_get(observation, "id")),
        _get(observation, "observed_at"),
        _correlation(investigation_id, invocation_id=invocation_ref),
        {
            "kind": _enum_token(_get(observation, "kind"), EvidenceKind) or "UNKNOWN",
            "object_type": object_type,
            "consultation_type": (
                _safe_consultation_type(_get(observation, "object_id"))
                if object_type == "advisor_call"
                else "UNKNOWN"
            ),
            "sha256": _sha_value(_get(observation, "sha256")),
            "source_version_sha256": _sha256_value(_get(observation, "source_version")),
            "invocation_ref": invocation_ref,
            "limitations_count": len(_get(observation, "limitations", ()) or ()),
            "content_included": False,
            "blob_ref": _uuid_value(_get(observation, "blob_id")),
        },
    )


def _finding_event(investigation_id: str, finding: Any) -> dict[str, Any]:
    return _event(
        "finding.version_recorded",
        _uuid_value(_get(finding, "id")),
        _get(finding, "created_at"),
        _correlation(investigation_id),
        {
            "kind": _enum_token(_get(finding, "kind"), FindingKind) or "UNKNOWN",
            "assessment": _enum_token(_get(finding, "assessment"), FindingAssessment) or "UNKNOWN",
            "novelty": _enum_token(_get(finding, "novelty"), NoveltyClass),
            "statement_sha256": _sha256_value(_get(finding, "statement")),
            "scope_sha256": _sha256_value(_get(finding, "scope")),
            "limitations_count": len(_get(finding, "limitations", ()) or ()),
            "observation_refs": sorted(
                _uuid_value(ref) for ref in (_get(finding, "observation_refs", ()) or ())
            ),
            "claim_refs": sorted(
                _opaque_id(ref) or "UNKNOWN" for ref in (_get(finding, "claim_refs", ()) or ())
            ),
            "supersedes": _uuid_value(_get(finding, "supersedes"))
            if _get(finding, "supersedes") is not None
            else None,
            "content_included": False,
        },
        version=_get(finding, "version"),
    )


def _experiment_event(investigation_id: str, experiment: Any) -> dict[str, Any]:
    invocation_refs = _get(experiment, "tool_invocation_refs", ()) or ()
    observation_refs = _get(experiment, "observation_refs", ()) or ()
    return _event(
        "experiment.version_recorded",
        _uuid_value(_get(experiment, "id")),
        _get(experiment, "recorded_at"),
        _correlation(investigation_id),
        {
            "hypothesis_id": _opaque_id(_get(experiment, "hypothesis_id")) or "UNKNOWN",
            "claim_ids": sorted(
                _opaque_id(value) or "UNKNOWN"
                for value in (_get(experiment, "claim_ids", ()) or ())
            ),
            "verdict": _enum_token(_get(experiment, "verdict"), ExperimentVerdict) or "UNKNOWN",
            "actual_outcome": _enum_token(_get(experiment, "actual_outcome"), ObservationOutcome)
            or "UNKNOWN",
            "observation_coverage": _enum_token(
                _get(experiment, "observation_coverage"), ObservationCoverage
            )
            or "UNKNOWN",
            "availability": _enum_token(_get(experiment, "availability"), ExperimentAvailability)
            or "UNKNOWN",
            "tool": _safe_tool_name(_get(experiment, "tool_name")),
            "tool_name_sha256": _sha256_value(_get(experiment, "tool_name")),
            "prediction_sha256": _sha256_value(_get(experiment, "prediction")),
            "expected_sha256": _sha256_value(_get(experiment, "expected")),
            "actual_sha256": _sha256_value(_get(experiment, "actual")),
            "falsification_criterion_sha256": _sha256_value(
                _get(experiment, "falsification_criterion")
            ),
            "negative_result": (
                _enum_token(_get(experiment, "actual_outcome"), ObservationOutcome)
                == ObservationOutcome.NOT_OBSERVED.value
            ),
            "invocation_refs": [
                {
                    "invocation_id": _opaque_id(_get(ref, "invocation_id")),
                    "version": _get(ref, "version"),
                }
                for ref in invocation_refs
            ],
            "observation_refs": [
                {
                    "observation_id": _uuid_value(_get(ref, "observation_id")),
                    "sha256": _sha_value(_get(ref, "sha256")),
                }
                for ref in observation_refs
            ],
            "content_included": False,
        },
        version=_get(experiment, "version"),
    )


def _opinion_events(investigation_id: str, opinion: Any) -> list[dict[str, Any]]:
    opinion_id = _uuid_value(_get(opinion, "id"))
    version = _get(opinion, "version")
    events: list[dict[str, Any]] = [
        _event(
            "opinion.version_recorded",
            opinion_id,
            _get(opinion, "created_at"),
            _correlation(investigation_id),
            {
                "status": _enum_token(_get(opinion, "status"), AnalystOpinionStatus) or "UNKNOWN",
                "content_sha256": _sha_value(_get(opinion, "content_sha256")),
                "synthesis_artifact_id": _uuid_value(_get(opinion, "synthesis_artifact_id")),
                "synthesis_sha256": _sha_value(_get(opinion, "synthesis_sha256")),
                "base_publication_artifact_id": _uuid_value(
                    _get(opinion, "base_publication_artifact_id")
                ),
                "base_publication_sha256": _sha_value(_get(opinion, "base_publication_sha256")),
                "block_count": len(_get(opinion, "blocks", ()) or ()),
                "content_included": False,
            },
            version=version,
        )
    ]
    for block in _get(opinion, "blocks", ()) or ():
        draft_ref = _get(block, "bridge_draft_ref")
        review_ref = _get(block, "bridge_review_ref")
        invocation_ids = []
        if draft_ref is not None:
            invocation_ids.append(_opaque_id(_get(draft_ref, "invocation_id")))
        if review_ref is not None:
            invocation_ids.append(_opaque_id(_get(review_ref, "invocation_id")))
        refs = [item for item in invocation_ids if item]
        event = _event(
            "opinion.block_decision",
            opinion_id,
            _get(opinion, "created_at"),
            _correlation(investigation_id, invocation_id=refs[0] if refs else None),
            {
                "opinion_id": opinion_id,
                "opinion_version": version,
                "opinion_status": (
                    _enum_token(_get(opinion, "status"), AnalystOpinionStatus) or "UNKNOWN"
                ),
                "block_type": _enum_token(_get(block, "block_type"), OpinionBlockType) or "UNKNOWN",
                "qualification": (
                    _enum_token(_get(block, "qualification"), OpinionClaimQualification)
                    or "UNKNOWN"
                ),
                "finding_refs": sorted(
                    _uuid_value(ref) for ref in (_get(block, "finding_refs", ()) or ())
                ),
                "evidence_refs": sorted(
                    _uuid_value(ref) for ref in (_get(block, "evidence_refs", ()) or ())
                ),
                "text_sha256": _sha256_value(_get(block, "text")),
                "bridge_draft_ref": (
                    {
                        "invocation_id": _opaque_id(_get(draft_ref, "invocation_id")),
                        "text_sha256": _sha_value(_get(draft_ref, "text_sha256")),
                    }
                    if draft_ref is not None
                    else None
                ),
                "bridge_review_ref": (
                    {
                        "invocation_id": _opaque_id(_get(review_ref, "invocation_id")),
                        "output_sha256": _sha_value(_get(review_ref, "output_sha256")),
                        "reviewed_text_sha256": _sha_value(
                            _get(review_ref, "reviewed_text_sha256")
                        ),
                    }
                    if review_ref is not None
                    else None
                ),
                "content_included": False,
            },
            version=version,
        )
        events.append(event)
    return events


def _candidate_event(investigation_id: str, candidate: Any) -> dict[str, Any]:
    motifs = _get(candidate, "motif_provenance", ()) or ()
    return _event(
        "detection_candidate.version_recorded",
        _uuid_value(_get(candidate, "id")),
        _get(candidate, "created_at"),
        _correlation(investigation_id),
        {
            "candidate_id": _uuid_value(_get(candidate, "candidate_id")),
            "version": _get(candidate, "version"),
            "status": _enum_token(_get(candidate, "status"), DetectionCandidateStatus) or "UNKNOWN",
            "parent_version_id": _uuid_value(_get(candidate, "parent_version_id"))
            if _get(candidate, "parent_version_id") is not None
            else None,
            "test_refs": sorted(
                _uuid_value(ref) for ref in (_get(candidate, "test_refs", ()) or ())
            ),
            "motif_provenance": [
                {
                    "motif_sha256": _sha256_value(_get(motif, "motif")),
                    "rationale_sha256": _sha256_value(_get(motif, "rationale")),
                    "observation_refs": sorted(
                        _uuid_value(ref) for ref in (_get(motif, "observation_refs", ()) or ())
                    ),
                }
                for motif in motifs
            ],
            "source_sha256": _sha_value(_get(candidate, "source_sha256")),
            "content_included": False,
        },
        version=_get(candidate, "version"),
    )


def _sample_association_event(investigation_id: str, association: Any) -> dict[str, Any]:
    changes = _get(association, "changes", ()) or ()
    return _event(
        "sample_association.version_recorded",
        _uuid_value(_get(association, "id")),
        _get(association, "created_at"),
        _correlation(investigation_id),
        {
            "version": _get(association, "version"),
            "origin": _enum_token(_get(association, "origin"), SampleOrigin) or "UNKNOWN",
            "qualification": _enum_token(_get(association, "qualification"), SampleQualification)
            or "UNKNOWN",
            "changes": [
                {
                    "sequence": sequence,
                    "from_qualification": (
                        _enum_token(_get(change, "from_qualification"), SampleQualification)
                        or "UNKNOWN"
                    ),
                    "to_qualification": (
                        _enum_token(_get(change, "to_qualification"), SampleQualification)
                        or "UNKNOWN"
                    ),
                    "actor_kind": (
                        _enum_token(_get(change, "actor_kind"), QualificationActorKind) or "UNKNOWN"
                    ),
                    "actor_id_sha256": _sha256_value(_get(change, "actor_id")),
                    "reason_sha256": _sha256_value(_get(change, "reason")),
                    "changed_at": _iso(_get(change, "changed_at")),
                }
                for sequence, change in enumerate(changes, start=1)
            ],
            "change_count": len(changes),
            "sample_sha256": "REDACTED",
            "content_included": False,
        },
        version=_get(association, "version"),
    )


def _blob_uuid(reference: Any) -> str | None:
    value = _text(reference)
    if not value:
        return None
    prefix, separator, identifier = value.partition("://")
    if separator and prefix == "blob":
        try:
            return str(UUID(identifier))
        except ValueError:
            return None
    return None


def _submission_projection(run: Any) -> dict[str, Any]:
    source_state = _enum_token(_get(run, "submission_state"), ModelSubmissionState)
    mapped = {
        "not_submitted": "not_sent",
        "external_state_unknown": "uncertain",
        "submission_in_progress": "uncertain",
        "result_obtained": "result_obtained",
        "verified_terminal_failure": "uncertain",
    }
    return {
        "state": mapped.get(source_state or "", "UNKNOWN"),
        "source_state": source_state or "UNKNOWN",
        "sent_confirmation": _unknown("ModelRun has no separate persisted 'sent' transition"),
    }


def _conversation_event(investigation_id: str, conversation: Any) -> dict[str, Any] | None:
    if conversation is None:
        return None
    conversation_id = _uuid_value(_get(conversation, "id"))
    return _event(
        "model_conversation.snapshot",
        conversation_id,
        _get(conversation, "created_at"),
        _correlation(investigation_id),
        {
            "relation": "analyst_investigations.pivot_conversation_id",
            "purpose": _enum_token(_get(conversation, "purpose"), ConversationPurpose) or "UNKNOWN",
            "provider": _enum_token(_get(conversation, "provider"), ModelProvider) or "UNKNOWN",
            "transport": _enum_token(_get(conversation, "transport"), ConversationTransport)
            or "UNKNOWN",
            "status": _enum_token(_get(conversation, "status"), ConversationStatus) or "UNKNOWN",
            "head_turn_id": _uuid_value(_get(conversation, "head_turn_id"))
            if _get(conversation, "head_turn_id") is not None
            else None,
            "turn_count": _get(conversation, "turn_count"),
            "version": _get(conversation, "version"),
            "external_locator": "OMITTED",
        },
        version=_get(conversation, "version"),
    )


def _turn_event(investigation_id: str, turn: Any) -> dict[str, Any]:
    model_run_id = (
        _uuid_value(_get(turn, "model_run_id")) if _get(turn, "model_run_id") is not None else None
    )
    turn_id = _uuid_value(_get(turn, "id"))
    return _event(
        "model_conversation.turn",
        turn_id,
        _get(turn, "created_at"),
        _correlation(investigation_id, model_run_id=model_run_id, turn_id=turn_id),
        {
            "conversation_id": _uuid_value(_get(turn, "conversation_id")),
            "sequence": _get(turn, "sequence"),
            "parent_turn_id": _uuid_value(_get(turn, "parent_turn_id"))
            if _get(turn, "parent_turn_id") is not None
            else None,
            "model_run_id": model_run_id,
            "status": _enum_token(_get(turn, "status"), ConversationTurnStatus) or "UNKNOWN",
            "idempotency_key": _idempotency_fingerprint(_get(turn, "idempotency_key")),
            "correlation_id": _idempotency_fingerprint(_get(turn, "correlation_id")),
            "external_turn_id": _opaque_id(_get(turn, "external_turn_id")),
            "input_sha256": _sha_value(_get(turn, "input_sha256")),
            "output_sha256": _sha_value(_get(turn, "output_sha256")),
            "input_blob_id": _blob_uuid(_get(turn, "input_blob_reference")),
            "output_blob_id": _blob_uuid(_get(turn, "output_blob_reference")),
            "content_included": False,
        },
    )


def _model_run_event(investigation_id: str, run: Any, turn_id: str | None) -> dict[str, Any]:
    usage = _get(run, "usage")
    usage_value = (
        usage.snapshot()
        if usage is not None and callable(getattr(usage, "snapshot", None))
        else _get(run, "usage")
    )
    if isinstance(usage_value, Mapping):
        usage_value = {
            key: value
            for key, value in usage_value.items()
            if key in {"input_tokens", "output_tokens", "total_tokens", "estimated"}
            and isinstance(value, (int, bool))
        }
    else:
        usage_value = _unknown("token usage was not persisted")
    run_id = _uuid_value(_get(run, "id"))
    return _event(
        "model_run.snapshot",
        run_id,
        _get(run, "started_at"),
        _correlation(investigation_id, model_run_id=run_id, turn_id=turn_id),
        {
            "provider": _enum_token(_get(run, "provider"), ModelProvider) or "UNKNOWN",
            "backend": _enum_token(_get(run, "backend"), ModelBackend) or "UNKNOWN",
            "transport": _enum_token(_get(run, "transport"), ModelTransport) or "UNKNOWN",
            "model_role": _enum_token(_get(run, "model_role"), ModelRole) or "UNKNOWN",
            "requested_model_sha256": _sha256_value(_get(run, "requested_model")),
            "actual_model_version_sha256": _sha256_value(_get(run, "actual_model_version")),
            "prompt_template_id_sha256": _sha256_value(_get(run, "prompt_template_id")),
            "prompt_template_version_sha256": _sha256_value(_get(run, "prompt_template_version")),
            "authorized_input_hash": _sha_value(_get(run, "authorized_input_hash")),
            "evidence_pack_hash": _sha_value(_get(run, "evidence_pack_hash")),
            "status": _enum_token(_get(run, "status"), ModelRunStatus) or "UNKNOWN",
            "submission": _submission_projection(run),
            "submission_attempt": _get(run, "submission_attempt"),
            "bridge_request_id": _opaque_id(_get(run, "bridge_request_id")),
            "response_id": _opaque_id(_get(run, "response_id")),
            "raw_output_sha256": _sha_value(_get(run, "raw_output_sha256")),
            "normalized_output_sha256": _sha_value(_get(run, "normalized_output_sha256")),
            "raw_output_blob_id": _blob_uuid(_get(run, "raw_output_reference")),
            "normalized_output_blob_id": _blob_uuid(_get(run, "normalized_output_reference")),
            "parser_stage_sha256": _sha256_value(_get(run, "parser_stage")),
            "serializer_version_sha256": _sha256_value(_get(run, "serializer_version")),
            "normalization_version_sha256": _sha256_value(_get(run, "normalization_version")),
            "reparse": _unknown("no separate reparse history is stored on ModelRun"),
            "duration_ms": _get(run, "duration_ms")
            if isinstance(_get(run, "duration_ms"), int)
            else "UNKNOWN",
            "usage": usage_value,
            "monetary_cost": _unknown("no monetary price estimate or charge is stored"),
            "raw_output_chars": _get(run, "raw_output_chars")
            if isinstance(_get(run, "raw_output_chars"), int)
            else "UNKNOWN",
            "content_included": False,
        },
        version=None,
    )


def _envelope_event(investigation_id: str, envelope: Any) -> dict[str, Any] | None:
    if envelope is None:
        return None
    disclosure = _get(envelope, "disclosure")
    return _event(
        "governance_envelope.snapshot",
        investigation_id,
        _get(envelope, "approved_at"),
        _correlation(investigation_id),
        {
            "version": _get(envelope, "version"),
            "allowed_providers": sorted(
                _enum_token(value, ModelProvider) or "UNKNOWN"
                for value in (_get(envelope, "allowed_providers", ()) or ())
            ),
            "tlp": _enum_token(_get(disclosure, "tlp"), TLPLevel) or "UNKNOWN",
            "do_not_submit": _boolean_or_unknown(
                _get(disclosure, "do_not_submit")
            ),
            "external_llm_allowed": _boolean_or_unknown(
                _get(disclosure, "external_llm_allowed")
            ),
            "approval_decision_id": _opaque_id(_get(envelope, "approval_decision_id")),
            "approval_actor_id": "REDACTED" if _get(envelope, "approval_actor_id") else None,
        },
        version=_get(envelope, "version"),
    )


def _budget_event(investigation_id: str, budget_snapshot: Any) -> dict[str, Any] | None:
    if budget_snapshot is None:
        return None
    budget = _get(budget_snapshot, "budget")
    reservations = _get(budget, "reservations", ()) or ()
    rendered = []
    for item in reservations:
        operation_key = _text(_get(item, "operation_key"))
        operation_key_fingerprint = _idempotency_fingerprint(operation_key)
        operation_key_ref = {
            "operation_key_sha256": operation_key_fingerprint["value"],
            "representation": operation_key_fingerprint["representation"],
        }
        rendered.append(
            (
                operation_key or "",
                {
                    **operation_key_ref,
                    "status_at_snapshot": (
                        _enum_token(_get(item, "status"), BudgetReservationStatus) or "UNKNOWN"
                    ),
                    "reserved_amounts": _budget_amounts(_get(item, "amounts", {})),
                    "committed_amounts": _budget_amounts(_get(item, "committed_amounts", {})),
                },
            )
        )
    rendered = [item for _, item in sorted(rendered, key=lambda pair: pair[0])]
    return _event(
        "budget.snapshot",
        investigation_id,
        None,
        _correlation(investigation_id),
        {
            "version": _get(budget_snapshot, "version"),
            "limits": _budget_amounts(_get(budget, "limits", {})),
            "reservations": rendered,
            "transition_timestamps": _unknown(
                "budget repository stores current versioned state, not transition timestamps"
            ),
        },
        version=_get(budget_snapshot, "version"),
    )


def project_audit_events(snapshot: AuditSourceSnapshot) -> list[dict[str, Any]]:
    """Build compact events using only known stored fields and explicit links."""
    investigation_id = _required_uuid(snapshot.investigation_id, "investigation_id")
    reservations = _reservation_map(snapshot.budget_snapshot)

    attachments: dict[str, list[dict[str, str]]] = {}
    for opinion in snapshot.opinions:
        opinion_id = _uuid_value(_get(opinion, "id"))
        for block in _get(opinion, "blocks", ()) or ():
            for ref_name in ("bridge_draft_ref", "bridge_review_ref"):
                ref = _get(block, ref_name)
                invocation_id = _opaque_id(_get(ref, "invocation_id")) if ref is not None else None
                if invocation_id and opinion_id != "UNKNOWN":
                    attachments.setdefault(invocation_id, []).append(
                        {"opinion_id": opinion_id, "version": str(_get(opinion, "version"))}
                    )
    for values in attachments.values():
        values.sort(key=lambda item: (item["opinion_id"], item["version"]))

    events = [
        _invocation_event(investigation_id, item, reservations, attachments)
        for item in snapshot.invocations
    ]
    events.extend(_observation_event(investigation_id, item) for item in snapshot.observations)
    events.extend(_finding_event(investigation_id, item) for item in snapshot.findings)
    events.extend(_experiment_event(investigation_id, item) for item in snapshot.experiments)
    events.extend(
        event for item in snapshot.opinions for event in _opinion_events(investigation_id, item)
    )
    events.extend(
        _candidate_event(investigation_id, item) for item in snapshot.detection_candidates
    )
    events.extend(
        _sample_association_event(investigation_id, item) for item in snapshot.sample_associations
    )
    conversation_event = _conversation_event(investigation_id, snapshot.conversation)
    if conversation_event is not None:
        events.append(conversation_event)
    events.extend(_turn_event(investigation_id, item) for item in snapshot.conversation_turns)
    turns_by_run = {
        _uuid_value(_get(item, "model_run_id")): _uuid_value(_get(item, "id"))
        for item in snapshot.conversation_turns
    }
    events.extend(
        _model_run_event(investigation_id, item, turns_by_run.get(_uuid_value(_get(item, "id"))))
        for item in snapshot.model_runs
    )
    state_event = _state_event(investigation_id, snapshot.hypotheses_state)
    if state_event is not None:
        events.append(state_event)
    envelope_event = _envelope_event(investigation_id, snapshot.envelope)
    if envelope_event is not None:
        events.append(envelope_event)
    budget_event = _budget_event(investigation_id, snapshot.budget_snapshot)
    if budget_event is not None:
        events.append(budget_event)

    events.sort(key=_event_sort_key)
    counts = dict(sorted(Counter(item["type"] for item in events).items()))
    source_digest = _sha256_bytes(_canonical_bytes(events))
    version_map = {
        key: expected
        for key, expected in _SOURCE_VERSION_ALLOWLIST.items()
        if snapshot.source_versions.get(key) == expected
    }
    events.append(
        _event(
            "audit.snapshot",
            investigation_id,
            None,
            _correlation(investigation_id),
            {
                "projected_source_sha256": source_digest,
                "source_versions": version_map,
                "direct_pivot_conversation_id": (
                    _uuid_value(snapshot.pivot_conversation_id)
                    if snapshot.pivot_conversation_id
                    else None
                ),
                "event_counts_before_snapshot": counts,
                "event_count_before_snapshot": len(events),
                "causality_policy": "explicit IDs link records; order is not causality",
            },
        )
    )
    return events


def _json_line(value: Any) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _gzip_events(events: Sequence[Mapping[str, Any]]) -> bytes:
    target = io.BytesIO()
    with gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        for item in events:
            stream.write(_json_line(item))
    return target.getvalue()


def _read_gzip_events(data: bytes, expected_investigation_id: str) -> list[dict[str, Any]]:
    expected_investigation_id = _required_uuid(expected_investigation_id, "investigation_id")
    try:
        raw = gzip.decompress(data)
        rows = [json.loads(line) for line in raw.splitlines() if line]
    except (OSError, EOFError, json.JSONDecodeError) as exc:
        raise ValueError("Existing RUN_EVENTS.jsonl.gz is incomplete or invalid") from exc
    if not rows or any(
        not isinstance(row, dict)
        or row.get("schema") != AUDIT_EVENT_SCHEMA
        or not isinstance(row.get("type"), str)
        or not isinstance(row.get("event_id"), str)
        or not isinstance(row.get("occurred_at"), (str, type(None)))
        or not isinstance(row.get("record"), Mapping)
        or not isinstance(row.get("record", {}).get("id"), str)
        or not isinstance(row.get("correlation"), Mapping)
        or not isinstance(row.get("details"), Mapping)
        for row in rows
    ):
        raise ValueError("Existing RUN_EVENTS.jsonl.gz has an unsupported event schema")
    if any(
        row.get("correlation", {}).get("investigation_id") != expected_investigation_id
        for row in rows
    ):
        raise ValueError("Existing RUN_EVENTS.jsonl.gz belongs to a different investigation")
    if rows[-1].get("type") != "audit.snapshot" or any(
        row.get("type") == "audit.snapshot" for row in rows[:-1]
    ):
        raise ValueError("Existing RUN_EVENTS.jsonl.gz has an invalid terminal snapshot")
    source_events = rows[:-1]
    for event in rows:
        event_body = {key: value for key, value in event.items() if key != "event_id"}
        if event["event_id"] != _stable_id(event_body):
            raise ValueError("Existing RUN_EVENTS.jsonl.gz contains a stale event ID")
    if source_events != sorted(source_events, key=_event_sort_key):
        raise ValueError("Existing RUN_EVENTS.jsonl.gz events are not in stable order")
    snapshot_details = rows[-1].get("details")
    if not isinstance(snapshot_details, Mapping):
        raise ValueError("Existing RUN_EVENTS.jsonl.gz has an invalid snapshot record")
    actual_counts = dict(sorted(Counter(row.get("type") for row in source_events).items()))
    expected_digest = _sha256_bytes(_canonical_bytes(source_events))
    if (
        snapshot_details.get("event_count_before_snapshot") != len(source_events)
        or snapshot_details.get("event_counts_before_snapshot") != actual_counts
        or snapshot_details.get("projected_source_sha256") != expected_digest
    ):
        raise ValueError("Existing RUN_EVENTS.jsonl.gz snapshot counts or digest are inconsistent")
    return rows


def _details(event: Mapping[str, Any]) -> Mapping[str, Any]:
    details = event.get("details")
    return details if isinstance(details, Mapping) else {}


def _resource_index(events: Sequence[Mapping[str, Any]], investigation_id: str) -> dict[str, Any]:
    resources: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        event_type = event["type"]
        record = event["record"]
        details = _details(event)
        if event_type == "evidence_observation.recorded":
            key = ("evidence_observation", str(record["id"]), "")
            resources[key] = {
                "kind": key[0],
                "id": key[1],
                "blob_id": details.get("blob_ref"),
                "sha256": details.get("sha256"),
                "retrieval": "canonical evidence observation and blob repositories",
                "authorization": "existing application authorization required",
                "included": False,
            }
        elif event_type == "finding.version_recorded":
            key = ("finding", str(record["id"]), str(record.get("version") or ""))
            resources[key] = {
                "kind": key[0],
                "id": key[1],
                "version": record.get("version"),
                "content_sha256": details.get("statement_sha256"),
                "retrieval": "canonical finding repository",
                "authorization": "existing application authorization required",
                "included": False,
            }
        elif event_type == "experiment.version_recorded":
            key = ("experiment", str(record["id"]), str(record.get("version") or ""))
            resources[key] = {
                "kind": key[0],
                "id": key[1],
                "version": record.get("version"),
                "retrieval": "canonical investigation experiment repository",
                "authorization": "existing application authorization required",
                "included": False,
            }
        elif event_type.startswith("opinion."):
            opinion_id = str(details.get("opinion_id") or record["id"])
            version = str(details.get("opinion_version") or record.get("version") or "")
            key = ("analyst_opinion", opinion_id, version)
            resources[key] = {
                "kind": key[0],
                "id": key[1],
                "version": version or None,
                "content_sha256": details.get("content_sha256"),
                "retrieval": "canonical analyst opinion repository",
                "authorization": "existing application authorization required",
                "included": False,
            }
            for artifact_kind, id_key, sha_key in (
                ("synthesis_artifact", "synthesis_artifact_id", "synthesis_sha256"),
                (
                    "base_publication_artifact",
                    "base_publication_artifact_id",
                    "base_publication_sha256",
                ),
            ):
                artifact_id = details.get(id_key)
                if artifact_id:
                    artifact_key = (artifact_kind, str(artifact_id), "")
                    resources[artifact_key] = {
                        "kind": artifact_kind,
                        "id": str(artifact_id),
                        "sha256": details.get(sha_key),
                        "retrieval": "canonical artifact repository",
                        "authorization": "existing application authorization required",
                        "included": False,
                    }
        elif event_type == "tool_invocation.snapshot":
            for ref in details.get("response_refs", []):
                key = ("tool_response_reference", str(ref), "")
                resources.setdefault(
                    key,
                    {
                        "kind": key[0],
                        "id": key[1],
                        "retrieval": "ToolInvocation response ref; target type may be UNKNOWN",
                        "authorization": "existing application authorization required",
                        "included": False,
                    },
                )
        elif event_type == "detection_candidate.version_recorded":
            key = ("detection_candidate", str(record["id"]), str(record.get("version") or ""))
            resources[key] = {
                "kind": key[0],
                "id": key[1],
                "version": record.get("version"),
                "candidate_id": details.get("candidate_id"),
                "source_sha256": details.get("source_sha256"),
                "test_refs": details.get("test_refs", []),
                "motif_provenance": details.get("motif_provenance", []),
                "retrieval": "canonical detection candidate repository",
                "authorization": "existing application authorization required",
                "included": False,
            }
            for test_ref in details.get("test_refs", []):
                test_key = ("detection_candidate_test_reference", str(test_ref), "")
                resources.setdefault(
                    test_key,
                    {
                        "kind": test_key[0],
                        "id": test_key[1],
                        "retrieval": (
                            "detection candidate test reference; target type may be UNKNOWN"
                        ),
                        "authorization": "existing application authorization required",
                        "included": False,
                    },
                )
        elif event_type == "model_conversation.snapshot":
            key = ("model_conversation", str(record["id"]), "")
            resources[key] = {
                "kind": key[0],
                "id": key[1],
                "retrieval": "canonical model conversation repository",
                "authorization": "existing application authorization required",
                "included": False,
            }
        elif event_type == "model_conversation.turn":
            key = ("model_conversation_turn", str(record["id"]), "")
            resources[key] = {
                "kind": key[0],
                "id": key[1],
                "input_blob_id": details.get("input_blob_id"),
                "input_sha256": details.get("input_sha256"),
                "output_blob_id": details.get("output_blob_id"),
                "output_sha256": details.get("output_sha256"),
                "model_run_id": details.get("model_run_id"),
                "retrieval": "canonical model conversation turn and blob repositories",
                "authorization": "existing application authorization required",
                "included": False,
            }
            for role in ("input", "output"):
                blob_id = details.get(f"{role}_blob_id")
                if blob_id:
                    blob_key = (f"model_{role}_blob", str(blob_id), "")
                    resources[blob_key] = {
                        "kind": blob_key[0],
                        "id": str(blob_id),
                        "sha256": details.get(f"{role}_sha256"),
                        "retrieval": "canonical blob repository",
                        "authorization": "existing application authorization required",
                        "included": False,
                    }
        elif event_type == "model_run.snapshot":
            key = ("model_run", str(record["id"]), "")
            resources[key] = {
                "kind": key[0],
                "id": key[1],
                "raw_output_sha256": details.get("raw_output_sha256"),
                "normalized_output_sha256": details.get("normalized_output_sha256"),
                "raw_output_blob_id": details.get("raw_output_blob_id"),
                "normalized_output_blob_id": details.get("normalized_output_blob_id"),
                "retrieval": "canonical ModelRun and blob repositories",
                "authorization": "existing application authorization required",
                "included": False,
            }
            for output_kind in ("raw_output", "normalized_output"):
                blob_id = details.get(f"{output_kind}_blob_id")
                if blob_id:
                    blob_key = (output_kind, str(blob_id), "")
                    resources[blob_key] = {
                        "kind": blob_key[0],
                        "id": str(blob_id),
                        "sha256": details.get(f"{output_kind}_sha256"),
                        "retrieval": "canonical blob repository",
                        "authorization": "existing application authorization required",
                        "included": False,
                    }

    has_turns = any(event["type"] == "model_conversation.turn" for event in events)
    has_runs = any(event["type"] == "model_run.snapshot" for event in events)
    unavailable = []
    if not has_turns:
        unavailable.append(
            {
                "kind": "model_conversation_message",
                "id": None,
                "availability": "UNKNOWN",
                "reason": "no directly linked pivot conversation turn exists for this snapshot",
            }
        )
    if not has_runs:
        unavailable.append(
            {
                "kind": "model_run_output",
                "id": None,
                "availability": "UNKNOWN",
                "reason": "no directly linked ModelRun exists for this snapshot",
            }
        )
    unavailable.extend(
        [
            {
                "kind": "tool_invocation_model_run_relation",
                "id": None,
                "availability": "UNKNOWN",
                "reason": "ToolInvocation has no direct ModelRun foreign key",
            },
            {
                "kind": "pdf_and_qa_artifact",
                "id": None,
                "availability": "UNKNOWN",
                "reason": "no PDF/QA artifact link is stored on the selected investigation records",
            },
        ]
    )
    return {
        "schema": AUDIT_INDEX_SCHEMA,
        "investigation_id": investigation_id,
        "policy": "references only; application authorization required; no signed URLs or payloads",
        "resources": [resources[key] for key in sorted(resources)],
        "unavailable_resources": unavailable,
    }


def _safe_summary(event: Mapping[str, Any]) -> str:
    kind = str(event["type"])
    record_id = str(event["record"]["id"])
    details = _details(event)
    if kind == "tool_invocation.snapshot":
        key = event["correlation"].get(
            "operation_key", event["correlation"].get("operation_key_sha256", "UNKNOWN")
        )
        return (
            f"tool={details.get('tool', 'UNKNOWN')} "
            f"status={details.get('status_at_snapshot', 'UNKNOWN')} operation_key={key}"
        )
    if kind == "evidence_observation.recorded":
        return (
            f"kind={details.get('kind', 'UNKNOWN')} "
            f"sha256={details.get('sha256', 'UNKNOWN')} "
            f"linked_invocation={event['correlation'].get('invocation_id', 'UNKNOWN')}"
        )
    if kind == "finding.version_recorded":
        return (
            f"assessment={details.get('assessment', 'UNKNOWN')} "
            f"version={event['record'].get('version')}"
        )
    if kind == "experiment.version_recorded":
        return (
            f"verdict={details.get('verdict', 'UNKNOWN')} "
            f"outcome={details.get('actual_outcome', 'UNKNOWN')} "
            f"coverage={details.get('observation_coverage', 'UNKNOWN')}"
        )
    if kind == "operator_state.snapshot":
        return (
            f"questions={len(details.get('questions', []))} "
            f"hypotheses={len(details.get('hypotheses', []))} "
            f"turn_index={details.get('turn_index', 'UNKNOWN')}"
        )
    if kind == "opinion.version_recorded":
        return (
            f"status={details.get('status', 'UNKNOWN')} "
            f"blocks={details.get('block_count', 'UNKNOWN')}"
        )
    if kind == "opinion.block_decision":
        return (
            f"opinion={details.get('opinion_id')} block={details.get('block_type')} "
            f"status={details.get('opinion_status')}"
        )
    if kind == "detection_candidate.version_recorded":
        return (
            f"candidate={details.get('candidate_id', 'UNKNOWN')} "
            f"status={details.get('status', 'UNKNOWN')}"
        )
    if kind == "sample_association.version_recorded":
        return (
            f"origin={details.get('origin', 'UNKNOWN')} "
            f"qualification={details.get('qualification', 'UNKNOWN')}"
        )
    if kind == "governance_envelope.snapshot":
        return (
            f"TLP={details.get('tlp', 'UNKNOWN')} "
            f"do_not_submit={details.get('do_not_submit', 'UNKNOWN')} "
            f"external_llm_allowed={details.get('external_llm_allowed', 'UNKNOWN')}"
        )
    if kind == "budget.snapshot":
        return (
            f"reservations={len(details.get('reservations', []))} "
            f"version={details.get('version', 'UNKNOWN')}"
        )
    return f"record={record_id}"


def _trace_bytes(events: Sequence[Mapping[str, Any]], investigation_id: str) -> bytes:
    lines = [
        "# Investigation audit trace",
        "",
        f"Investigation: `{investigation_id}`",
        "",
        (
            "This trace summarizes versioned records. It contains no full message, prompt, "
            "response, source passage, sample value, signed URL, or credential."
        ),
        (
            "Events use recorded timestamps when available. Time order does not establish "
            "causality; only explicit identifiers below create links."
        ),
        "",
        "## Chronology",
        "",
    ]
    for event in events:
        if event["type"] == "audit.snapshot":
            continue
        timestamp = event.get("occurred_at") or "timestamp UNKNOWN"
        lines.append(
            f"- `{timestamp}` **{event['type']}** `{event['record']['id']}` — "
            f"{_safe_summary(event)}."
        )
    lines.extend(["", "## Questions and hypotheses", ""])
    for event in events:
        details = _details(event)
        if event["type"] != "operator_state.snapshot":
            continue
        for question in details.get("questions", []):
            lines.append(
                f"- Question rank {question.get('rank', 'UNKNOWN')}: "
                f"sha256 `{question.get('question_sha256', 'UNKNOWN')}`."
            )
        for hypothesis in details.get("hypotheses", []):
            lines.append(
                f"- Hypothesis `{hypothesis.get('id', 'UNKNOWN')}`: "
                f"{hypothesis.get('status', 'UNKNOWN')}; statement sha256 "
                f"`{hypothesis.get('statement_sha256', 'UNKNOWN')}`; "
                "reason UNKNOWN."
            )
    lines.extend(["", "## Findings and experiment outcomes", ""])
    for event in events:
        details = _details(event)
        if event["type"] == "finding.version_recorded":
            lines.append(
                f"- Finding `{event['record']['id']}`: "
                f"{details.get('assessment', 'UNKNOWN')} "
                f"({details.get('kind', 'UNKNOWN')}); observation refs "
                f"{len(details.get('observation_refs', []))}."
            )
        elif event["type"] == "experiment.version_recorded":
            result_type = "negative" if details.get("negative_result") else "recorded"
            lines.append(
                f"- Experiment `{event['record']['id']}` ({result_type}): "
                f"verdict {details.get('verdict', 'UNKNOWN')}, outcome "
                f"{details.get('actual_outcome', 'UNKNOWN')}, coverage "
                f"{details.get('observation_coverage', 'UNKNOWN')}."
            )
    lines.extend(["", "## Sample qualification and detection candidates", ""])
    for event in events:
        details = _details(event)
        if event["type"] == "sample_association.version_recorded":
            lines.append(
                f"- Sample association `{event['record']['id']}`: "
                f"{details.get('qualification', 'UNKNOWN')}; "
                f"{details.get('change_count', 0)} persisted qualification change(s)."
            )
            for change in details.get("changes", []):
                lines.append(
                    f"  - {change.get('from_qualification', 'UNKNOWN')} -> "
                    f"{change.get('to_qualification', 'UNKNOWN')} at "
                    f"{change.get('changed_at') or 'timestamp UNKNOWN'} by "
                    f"{change.get('actor_kind', 'UNKNOWN')}; reason sha256 "
                    f"`{change.get('reason_sha256') or 'UNKNOWN'}`."
                )
        elif event["type"] == "detection_candidate.version_recorded":
            motifs = details.get("motif_provenance", [])
            lines.append(
                f"- Detection candidate `{details.get('candidate_id', 'UNKNOWN')}` "
                f"v{details.get('version', 'UNKNOWN')}: {details.get('status', 'UNKNOWN')}; "
                f"{len(details.get('test_refs', []))} test ref(s), "
                f"{len(motifs)} motif provenance record(s); source sha256 "
                f"`{details.get('source_sha256', 'UNKNOWN')}`."
            )
    lines.extend(["", "## Opinion decisions and direct links", ""])
    for event in events:
        details = _details(event)
        if event["type"] == "opinion.block_decision":
            invocation_id = event["correlation"].get("invocation_id", "UNKNOWN")
            lines.append(
                f"- Opinion `{details.get('opinion_id')}` v{details.get('opinion_version')}: "
                f"{details.get('block_type', 'UNKNOWN')} / "
                f"{details.get('opinion_status', 'UNKNOWN')}; Bridge invocation "
                f"{invocation_id}."
            )
        elif event["type"] == "model_conversation.turn":
            lines.append(
                f"- Turn `{event['record']['id']}` -> ModelRun "
                f"`{details.get('model_run_id')}`; parent turn "
                f"`{details.get('parent_turn_id') or 'UNKNOWN'}`."
            )
    lines.extend(
        [
            "",
            "## Explicit decisions and alternatives",
            "",
            (
                "Questions, hypothesis statuses, findings, experiments, opinion blocks, and direct "
                "invocation references appear as hashes, IDs, enums, or counts in "
                "`RUN_EVENTS.jsonl.gz`. Source text is omitted; recover it through the canonical "
                "application record and its authorization checks."
            ),
            "",
            (
                "Hypothesis status is recorded; a transition reason is UNKNOWN unless a structured "
                "reason exists. Negative experiment results include persisted coverage. Absence of "
                "an observation is not treated as a negative result."
            ),
            "",
            "## Transport and material completeness",
            "",
            (
                "Dossier, rendered prompt, observed HTTP payload, raw response, normalized output, "
                "redacted copy, consultation ID, Bridge invocation ID inside advisor-call blobs, "
                "model run ID, turn ID, and Bridge run ID are "
                "UNKNOWN unless a direct persisted record exists. This schema does not store the "
                "six material-stage hashes or Bridge prompt-as-file/prompt-bytes observations. "
                "The index lists the advisor-call observation blob but does not unpack its inner "
                "prompt/output artifact references."
            ),
            (
                "Admission amounts and current reservation status are separate. Submission state, "
                "parser version, reparse result, attachment outcome, latency, and cost are "
                "never inferred from event order or tool status."
            ),
            "",
            "## Retrieval index",
            "",
            (
                "See `RUN_INDEX.json` for canonical record/blob references. It carries no payloads "
                "or signed URLs; normal application authorization is required to retrieve content."
            ),
            "",
        ]
    )
    return ("\n".join(lines)).encode("utf-8")


def _manifest_bytes(
    events_bytes: bytes,
    trace_bytes: bytes,
    index_bytes: bytes,
    events: Sequence[Mapping[str, Any]],
    index: Mapping[str, Any],
    investigation_id: str,
) -> bytes:
    counts = Counter(event["type"] for event in events)
    snapshot = next((event for event in events if event["type"] == "audit.snapshot"), None)
    details = _details(snapshot or {})
    record_refs = [
        {
            "event_id": event["event_id"],
            "type": event["type"],
            "id": event["record"]["id"],
            "version": event["record"].get("version"),
        }
        for event in events
        if event["type"] != "audit.snapshot"
    ]
    gaps = [
        {
            "field": "consultation_id",
            "value": "UNKNOWN",
            "reason": "no consultation row is present in canonical schema",
        },
        {
            "field": "bridge_invocation_id",
            "value": "UNKNOWN when only present inside the referenced advisor-call blob",
            "reason": (
                "standard audit reader projects observation metadata and hashes, but does not load "
                "operator-advisor-call.v1 blob contents"
            ),
        },
        {
            "field": "advisor_call_embedded_artifact_refs",
            "value": "UNKNOWN",
            "reason": (
                "standard audit reader indexes the containing observation blob without unpacking it"
            ),
        },
        {
            "field": "invocation_to_model_run",
            "value": "UNKNOWN",
            "reason": (
                "No ToolInvocation-to-ModelRun foreign key; a pivot conversation may link both "
                "records independently"
            ),
        },
        {
            "field": "turn_id",
            "value": "UNKNOWN when no pivot conversation is linked",
            "reason": (
                "Investigation persistence exposes only pivot_conversation_id, not general "
                "conversation membership"
            ),
        },
        {
            "field": "bridge_run_id",
            "value": "UNKNOWN",
            "reason": "no durable Bridge-run record is linked to this investigation",
        },
        {
            "field": "prompt_as_file_and_prompt_bytes",
            "value": "UNKNOWN",
            "reason": "Bridge prompt file/byte observations are not persisted",
        },
        {
            "field": "rendered_prompt_and_http_payload_hashes",
            "value": "UNKNOWN",
            "reason": "No stage-specific prompt/payload hash is linked to ToolInvocation",
        },
        {
            "field": "tool_invocation_raw_and_normalized_outputs",
            "value": "UNKNOWN",
            "reason": (
                "No direct ModelRun/output link on ToolInvocation; linked pivot ModelRuns report "
                "their own raw and normalized hashes"
            ),
        },
        {
            "field": "reparse_and_monetary_cost",
            "value": "UNKNOWN",
            "reason": "no separate reparse history or monetary price/charge is stored",
        },
        {
            "field": "objective_and_decision_reasons",
            "value": "UNKNOWN",
            "reason": "State stores questions/statuses, not a separate objective/reason",
        },
        {
            "field": "parent_child_sessions",
            "value": "UNKNOWN",
            "reason": "investigation persistence has no session hierarchy",
        },
        {
            "field": "pdf_and_qa",
            "value": "UNKNOWN",
            "reason": "no PDF/QA artifact link is present on the selected investigation rows",
        },
    ]
    payload = {
        "schema": AUDIT_MANIFEST_SCHEMA,
        "investigation_id": investigation_id,
        "source_snapshot": {
            "projected_source_sha256": details.get("projected_source_sha256"),
            "versions": details.get("source_versions", {}),
            "pivot_conversation_id": details.get("direct_pivot_conversation_id"),
            "record_refs": record_refs,
            "idempotency": "SHA-256 of stable projected events; no primary record is created",
        },
        "files": {
            "RUN_EVENTS.jsonl.gz": {
                "sha256": _sha256_bytes(events_bytes),
                "bytes": len(events_bytes),
                "records": len(events),
            },
            "RUN_TRACE.md": {"sha256": _sha256_bytes(trace_bytes), "bytes": len(trace_bytes)},
            "RUN_INDEX.json": {
                "sha256": _sha256_bytes(index_bytes),
                "bytes": len(index_bytes),
                "resources": len(index.get("resources", [])),
            },
            "RUN_MANIFEST.json": {
                "self_sha256": "NOT_COMPUTED",
                "reason": "manifest cannot contain its own stable digest",
            },
        },
        "counts": {
            "events": len(events),
            "by_type": dict(sorted(counts.items())),
            "resources": len(index.get("resources", [])),
            "unavailable_resources": len(index.get("unavailable_resources", [])),
        },
        "correlation_keys": [
            "investigation_id",
            "invocation_id",
            "operation_key_sha256",
            "consultation_id",
            "model_run_id",
            "turn_id",
            "bridge_run_id",
        ],
        "gaps": gaps,
        "redaction_policy": {
            "standard_export": (
                "Allowlisted IDs, statuses, counts, timestamps, and hashes only. No source/model "
                "text, normalized parameters, sample values, secrets, signed URLs, or raw payloads."
            ),
            "content_retrieval": (
                "Application authorization remains required; index references are not bearer "
                "capabilities"
            ),
            "gzip": "compression only; gzip is not a confidentiality control",
        },
        "resume_policy": (
            "RUN_EVENTS.jsonl.gz is atomically written first as the checkpoint. --resume rebuilds "
            "remaining files without rereading the database"
        ),
        "ordering_policy": "Stable timestamp/type/record/event-id order; time is not causality",
    }
    return _canonical_bytes(payload) + b"\n"


def build_audit_bundle(snapshot: AuditSourceSnapshot) -> AuditBundle:
    investigation_id = _required_uuid(snapshot.investigation_id, "investigation_id")
    events = project_audit_events(snapshot)
    events_bytes = _gzip_events(events)
    index_value = _resource_index(events, investigation_id)
    index_bytes = _canonical_bytes(index_value) + b"\n"
    trace_bytes = _trace_bytes(events, investigation_id)
    manifest_bytes = _manifest_bytes(
        events_bytes, trace_bytes, index_bytes, events, index_value, investigation_id
    )
    return AuditBundle(events_bytes, trace_bytes, index_bytes, manifest_bytes, len(events))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def _bundle_lock(directory: Path) -> Iterator[None]:
    """Serialize bundle replacement and checkpoint resume for one output directory."""
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".RUN_AUDIT_EXPORT.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _bundle_from_checkpoint(events_bytes: bytes, investigation_id: str) -> AuditBundle:
    events = _read_gzip_events(events_bytes, investigation_id)
    index_value = _resource_index(events, investigation_id)
    index_bytes = _canonical_bytes(index_value) + b"\n"
    trace_bytes = _trace_bytes(events, investigation_id)
    manifest_bytes = _manifest_bytes(
        events_bytes, trace_bytes, index_bytes, events, index_value, investigation_id
    )
    return AuditBundle(events_bytes, trace_bytes, index_bytes, manifest_bytes, len(events), True)


def write_audit_bundle(
    snapshot: AuditSourceSnapshot | None,
    output_dir: str | Path,
    investigation_id: str,
    *,
    resume: bool = False,
) -> AuditBundle:
    """Write one coherent bundle; resume from the complete events checkpoint if present."""
    investigation_id = _required_uuid(investigation_id, "investigation_id")
    directory = Path(output_dir)
    with _bundle_lock(directory):
        events_path = directory / "RUN_EVENTS.jsonl.gz"
        if resume and events_path.is_file():
            bundle = _bundle_from_checkpoint(events_path.read_bytes(), investigation_id)
        else:
            if snapshot is None:
                raise ValueError(
                    "A canonical snapshot is required when no events checkpoint exists"
                )
            if (
                _required_uuid(snapshot.investigation_id, "snapshot investigation_id")
                != investigation_id
            ):
                raise ValueError("Snapshot investigation ID does not match export target")
            bundle = build_audit_bundle(snapshot)
            _atomic_write(events_path, bundle.events)
        _atomic_write(directory / "RUN_TRACE.md", bundle.trace)
        _atomic_write(directory / "RUN_INDEX.json", bundle.index)
        _atomic_write(directory / "RUN_MANIFEST.json", bundle.manifest)
        return bundle


def load_audit_events(path: str | Path, investigation_id: str) -> list[dict[str, Any]]:
    """Load and validate a prior atomic event checkpoint."""
    return _read_gzip_events(
        Path(path).read_bytes(), _required_uuid(investigation_id, "investigation_id")
    )

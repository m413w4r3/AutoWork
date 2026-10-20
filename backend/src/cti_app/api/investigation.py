"""HTTP API for launching and reading exact-synthesis investigations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid5

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cti_app.application.analyst_handoff import (
    AnalystHandoffPolicy,
    AnalystPostSynthesisService,
    analyst_handoff_policy_from_sources,
    current_verified_extraction_inputs,
    loop_budget_from_settings,
)
from cti_app.application.blob_storage import BlobReadLimitExceededError
from cti_app.application.bridge_investigator import make_investigation_turn_idempotency_key
from cti_app.application.diagram_compilation import DiagramCompilationError
from cti_app.application.evidence_recorder import sanitize_evidence_text
from cti_app.application.identity import IdentityProvider, IdentityScope
from cti_app.application.investigation_composition import (
    InvestigationComposition,
    InvestigationCompositionFactory,
)
from cti_app.application.investigation_contracts import (
    AdvisorGuidanceV1,
    InvestigationOperatorContextV1,
    OperatorContextSectionPageV1,
    OperatorToolInvocationRequestV1,
    OperatorToolInvocationResponseV1,
    OperatorToolInvocationViewV1,
)
from cti_app.application.investigation_experiments import (
    build_investigation_experiment_timeline,
)
from cti_app.application.investigation_governance import (
    EnvelopeDecisionConflictError,
    EnvelopeNotFoundError,
    EnvelopeRevokedError,
    EnvelopeVersionConflictError,
    InvestigationActor,
    InvestigationEnvelopeProposal,
    InvestigationEnvelopeService,
    SampleAssociationNotFoundError,
    SampleQualificationEvidenceError,
    SampleQualificationPermissionError,
    SampleQualificationService,
    g2_envelope_authorization_state,
)
from cti_app.application.investigation_jobs import (
    INVESTIGATION_RUN_AGGREGATE_TYPE,
    InvestigationRunParameters,
)
from cti_app.application.investigation_operator_view import (
    InvestigationOperatorErrorCode,
    build_investigation_operator_view,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_pivot_frontier import (
    build_pivot_frontier,
    frontier_to_dict,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.investigation_state import InvestigationStateVersionConflict
from cti_app.application.investigation_tools import (
    ToolExecutionContext,
    project_tool_availability,
)
from cti_app.application.jobs import DuplicateJobError
from cti_app.application.network_protocol_model import render_protocol_model
from cti_app.application.operator_advisor_guidance import build_advisor_guidance
from cti_app.application.operator_context_summary import (
    InvestigationOperatorContextSummaryV1,
    OperatorContextItemPageV1,
)
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.protocol_diagram_projection import (
    ProtocolDiagramProjectionError,
    compile_protocol_render_preview,
)
from cti_app.application.research_context_builder import (
    MAX_OPERATOR_REQUEST_BYTES,
    MAX_OPERATOR_REQUESTS,
    USER_REQUEST_ACTION_PREFIX,
    CanonicalEvidenceSummaryPort,
    InvestigationContextTooLarge,
    blocked_capabilities_from_history,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    FindingAssessment,
    InvestigationBudget,
    InvestigationExecutionMode,
    QualificationActorKind,
    SampleQualification,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_experiment import (
    EXPERIMENT_TIMELINE_SCHEMA_VERSION,
    ConsolidatedClaimStatus,
    ExperimentAvailability,
    ExperimentMethodMode,
    ExperimentVerdict,
    ObservationCoverage,
    ObservationOutcome,
)
from cti_app.domain.investigation_pivot_frontier import (
    PIVOT_FRONTIER_SCHEMA_VERSION,
    PivotDisclosureState,
    PivotExecutionState,
    PivotG2State,
    PivotQualificationLevel,
)
from cti_app.domain.investigation_state import (
    InvestigationWorkingState,
    StopReason,
    investigation_abandoned_turn_id,
    investigation_bridge_attempt,
    investigation_orphaned_turn_id,
)
from cti_app.domain.jobs import INVESTIGATION_RUN_JOB_KIND, Job, JobStatus
from cti_app.domain.production import (
    AnalystInvestigation,
    AnalystInvestigationStatus,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.protocol_model import ProtocolModelV1
from cti_app.domain.retrohunt import RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api", tags=["investigations"])
_logger = logging.getLogger(__name__)
_SHA256 = r"^[0-9a-f]{64}$"
_BASE64_BLOB_TEXT = re.compile(r"^[A-Za-z0-9+/]{256,}={0,2}$")
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")
_USER_REQUEST_NAMESPACE = UUID("92e23e22-b681-49c9-87dd-a7fd13398c05")
_USER_REQUEST_MARKER_PREFIX = "api:user-request:"
_SIMILARITY_BUCKET = "investigation-sample-similarity"
_PROTOCOL_MODEL_BUCKET = "network-protocol-models"


class InvestigationStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str = Field(pattern=_SHA256)
    execution_mode: InvestigationExecutionMode | None = None


class InvestigationActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class EnvelopeDisclosureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tlp: str | None = Field(default=None, max_length=32)
    do_not_submit: bool | None = None
    external_llm_allowed: bool | None = None


class EnvelopeDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    allowed_providers: tuple[str, ...] = Field(default=(), max_length=8)
    budget_limits: dict[str, int] = Field(default_factory=dict, max_length=16)
    acquire_hits_allowed: bool | None = None
    max_acquire_volume: int | None = Field(default=None, ge=0)
    disclosure: EnvelopeDisclosureRequest = Field(default_factory=EnvelopeDisclosureRequest)
    reason: str = Field(min_length=1, max_length=4_000)
    correlation_id: str = Field(min_length=1, max_length=80)


class EnvelopeView(BaseModel):
    investigation_id: UUID
    approved: bool
    version: int
    allowed_providers: list[str]
    available_providers: list[str]
    acquire_hits_allowed: bool
    max_acquire_volume: int
    budget: dict[str, BudgetDimensionView]
    disclosure: dict[str, Any]
    approval_decision_id: UUID | None = None
    approval_actor_id: str | None = None
    approved_at: datetime | None = None


class InvestigationRequestTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(hash|ioc|claim_id)$")
    value: str = Field(min_length=1, max_length=256)


class InvestigationUserRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2_000)
    target: InvestigationRequestTarget | None = None
    expected_version: int | None = Field(default=None, ge=1)


class SampleQualificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    qualification: SampleQualification
    reason: str = Field(min_length=1, max_length=4_000)
    evidence_refs: tuple[UUID, ...] = Field(default=(), max_length=128)
    correlation_id: str = Field(min_length=1, max_length=80)


class EnvelopeRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4_000)
    correlation_id: str = Field(min_length=1, max_length=80)


class QualificationChangeView(BaseModel):
    from_qualification: SampleQualification
    to_qualification: SampleQualification
    actor_kind: QualificationActorKind
    actor_id: str
    reason: str
    changed_at: datetime


class AgendaItemView(BaseModel):
    question: str
    potential: float
    rank: int


class HypothesisView(BaseModel):
    id: str
    statement: str
    status: str
    evidence_refs: list[UUID]
    finding_refs: list[UUID]


class FindingRevisionView(BaseModel):
    id: UUID
    version: int
    created_at: datetime


class FindingView(BaseModel):
    id: UUID
    logical_id: UUID
    current_version_id: UUID
    version: int
    revision_chain: list[FindingRevisionView]
    kind: str
    assessment: FindingAssessment
    novelty: str | None
    statement: str
    scope: str
    limitations: list[str]
    observation_refs: list[UUID]
    claim_refs: list[str]
    created_at: datetime


class ExperimentInputRefView(BaseModel):
    input_type: str
    input_id: str
    sha256: str | None


class ExperimentInvocationRefView(BaseModel):
    invocation_id: UUID
    version: int


class ExperimentObservationRefView(BaseModel):
    observation_id: UUID
    sha256: str


class InvestigationExperimentViewV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^investigation-experiment\.v1$")
    id: UUID
    investigation_id: UUID
    version: int
    hypothesis_id: str
    claim_ids: list[str]
    working_state_version: int
    g2_envelope_version: int
    prediction: str
    falsification_criterion: str
    method: str
    method_mode: ExperimentMethodMode
    input_type: str
    input_refs: list[ExperimentInputRefView]
    preconditions: list[str]
    availability: ExperimentAvailability
    tool_name: str
    tool_version: str
    tool_schema_version: str
    tool_arguments: dict[str, Any]
    step_ids: list[str]
    tool_invocation_refs: list[ExperimentInvocationRefView]
    observation_refs: list[ExperimentObservationRefView]
    expected: str
    actual: str
    actual_outcome: ObservationOutcome
    observation_coverage: ObservationCoverage
    started_at: datetime
    completed_at: datetime
    recorded_at: datetime
    scope: str
    limitations: list[str]
    verdict: ExperimentVerdict
    supersedes_experiment_ids: list[UUID] = Field(default_factory=list)
    superseded_by: list[UUID] = Field(default_factory=list)
    implication: str
    next_pivots: list[str]
    author: str
    origin: str


class ClaimExperimentScopeViewV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    experiment_id: UUID
    scope: str
    verdict: ExperimentVerdict
    observation_ids: list[UUID]
    superseded_by: list[UUID] = Field(default_factory=list)


class ConsolidatedClaimVerdictViewV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    claim_id: str
    status: ConsolidatedClaimStatus
    verdicts: list[ExperimentVerdict]
    experiment_ids: list[UUID]
    scopes: list[ClaimExperimentScopeViewV1]
    contradictory: bool
    superseded_by: list[UUID] = Field(default_factory=list)


class InvestigationExperimentTimelineResponseV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: str = Field(
        default=EXPERIMENT_TIMELINE_SCHEMA_VERSION,
        pattern=r"^investigation-experiment-timeline\.v1$",
    )
    investigation_id: UUID
    stream_version: int
    experiments: list[InvestigationExperimentViewV1]
    claims: list[ConsolidatedClaimVerdictViewV1]


class ObservationView(BaseModel):
    id: UUID
    kind: str
    sha256: str
    source: str
    source_version: str
    observed_at: datetime
    object_type: str
    object_id: str
    limitations: list[str]
    invocation_ref: UUID | None


class PivotPriorityFactorsView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    novelty_vs_article: int
    novelty_vs_verified: int
    depth: int
    cost: int
    formula: str


class PivotFrontierNodeView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_id: str = Field(min_length=1, max_length=256)
    indicator_type: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=2_048)
    parent_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    path: list[str]
    source_observation_id: str | None
    source: str = Field(min_length=1, max_length=256)
    source_version: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    level: PivotQualificationLevel
    reason: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1)
    limitations: list[str]
    novelty_vs_article: bool
    novelty_vs_verified: bool
    estimated_cost: int = Field(ge=0)
    g2_state: PivotG2State
    disclosure_state: PivotDisclosureState
    priority_score: int = Field(ge=0, le=100)
    priority_factors: PivotPriorityFactorsView
    state: PivotExecutionState
    state_reason: str = Field(min_length=1, max_length=512)
    cursor: str | None = Field(default=None, max_length=2_048)
    pagination_complete: bool | None
    children_observed: list[str]


class PivotFrontierEdgeView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    child_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation: str = Field(min_length=1, max_length=128)
    source_observation_id: str
    source_version: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1)
    limitations: list[str]


class PivotFrontierPathView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_ids: list[str]
    edge_ids: list[str]


class PivotFrontierResponseV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: str = Field(
        default=PIVOT_FRONTIER_SCHEMA_VERSION,
        pattern=r"^investigation-pivot-frontier\.v1$",
    )
    investigation_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frontier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nodes: list[PivotFrontierNodeView]
    edges: list[PivotFrontierEdgeView]
    paths: list[PivotFrontierPathView]
    truncated: bool
    next_cursor: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class OperatorObservationPayloadResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    observation_id: UUID
    sha256: str
    offset: int
    length: int
    total_chars: int
    next_offset: int | None
    payload: str
    untrusted_data: bool = True


class OperatorToolInvocationApiResponseV1(OperatorToolInvocationResponseV1):
    output: dict[str, Any]


_OBSERVATION_SECRET_FIELD = re.compile(
    r"(?i)(?:api[_-]?key|private[_-]?key|access[_-]?token|client[_-]?secret|"
    r"authorization|credential|password|secret|signature|signed[_-]?url)"
)
_OBSERVATION_BINARY_FIELD = re.compile(
    r"(?i)(?:raw[_-]?(?:bytes|binary|octets|file|sample|payload)|"
    r"bytes[_-]?(?:raw|file|sample|payload))"
)
_OBSERVATION_TEXT_CHUNK_OMITTED = "[encoded binary content omitted]"
_LOCAL_TEXT_CHUNK_MAX_BYTES = 48 * 1024
_DECOMPILE_TEXT_CHUNK_MAX_BYTES = 4 * 1024 * 1024


class _SensitiveObservationTextError(ValueError):
    """Raised when an otherwise readable tool transcript contains redacted data."""


def _observation_base64_text_chunk_spec(
    value: Mapping[str, Any], *, allow_confined_text_chunks: bool
) -> tuple[str, str, str, int] | None:
    if not allow_confined_text_chunks:
        return None
    if (
        value.get("schema") == "local-command-execution.v1"
        and value.get("command_id") == "xxd"
        and value.get("output_encoding") == "base64-utf8"
    ):
        return ("output_chunks", "output_byte_length", "output_sha256", _LOCAL_TEXT_CHUNK_MAX_BYTES)
    if (
        value.get("schema_version") == "dotnet-analysis-observation.v1"
        and value.get("tool") == "method.decompile"
        and value.get("result_json_encoding") == "base64-utf8"
    ):
        return (
            "result_json_chunks",
            "result_json_byte_length",
            "result_json_sha256",
            _DECOMPILE_TEXT_CHUNK_MAX_BYTES,
        )
    return None


def _verified_base64_text_chunks(
    chunks: Any,
    *,
    byte_length: Any,
    sha256: Any,
    max_bytes: int,
) -> list[str]:
    if (
        not isinstance(chunks, list)
        or len(chunks) > 512
        or isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or byte_length < 0
        or byte_length > max_bytes
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or any(not isinstance(chunk, str) for chunk in chunks)
    ):
        return [_OBSERVATION_TEXT_CHUNK_OMITTED]

    encoded_length = sum(len(chunk) for chunk in chunks)
    if encoded_length > ((max_bytes + 2) // 3) * 4:
        return [_OBSERVATION_TEXT_CHUNK_OMITTED]
    try:
        encoded = "".join(chunks).encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
        text = decoded.decode("utf-8", errors="strict")
    except (binascii.Error, UnicodeError, ValueError):
        return [_OBSERVATION_TEXT_CHUNK_OMITTED]
    if (
        len(decoded) != byte_length
        or hashlib.sha256(decoded).hexdigest() != sha256
        or len(decoded) > max_bytes
        or any(ord(char) < 0x20 and char not in "\t\n\r" for char in text)
        or "\x7f" in text
    ):
        return [_OBSERVATION_TEXT_CHUNK_OMITTED]

    if sanitize_evidence_text(text, max_chars=max_bytes) != text:
        raise _SensitiveObservationTextError("Decoded tool text contains redacted content")
    return list(chunks)


def _sanitize_observation_payload(
    value: Any, *, depth: int = 0, allow_confined_text_chunks: bool = False
) -> Any:
    """Mask long encoded blobs while retaining bounded, readable evidence facts."""
    if depth > 16:
        return "[maximum depth omitted]"
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        chunk_spec = (
            _observation_base64_text_chunk_spec(
                value, allow_confined_text_chunks=allow_confined_text_chunks
            )
            if depth == 0
            else None
        )
        for key, item in list(value.items())[:128]:
            safe_key = str(key)[:256]
            if _OBSERVATION_SECRET_FIELD.search(safe_key) or _OBSERVATION_BINARY_FIELD.search(
                safe_key
            ):
                continue
            if chunk_spec is not None and safe_key == chunk_spec[0]:
                safe[safe_key] = _verified_base64_text_chunks(
                    item,
                    byte_length=value.get(chunk_spec[1]),
                    sha256=value.get(chunk_spec[2]),
                    max_bytes=chunk_spec[3],
                )
            else:
                safe[safe_key] = _sanitize_observation_payload(
                    item,
                    depth=depth + 1,
                    allow_confined_text_chunks=allow_confined_text_chunks,
                )
        return safe
    if isinstance(value, list):
        return [
            _sanitize_observation_payload(
                item, depth=depth + 1, allow_confined_text_chunks=allow_confined_text_chunks
            )
            for item in value[:512]
        ]
    if isinstance(value, str):
        safe_text = sanitize_evidence_text(value, max_chars=8 * 1024 * 1024)
        if _BASE64_BLOB_TEXT.fullmatch(safe_text):
            return "[encoded binary content omitted]"
        return safe_text
    if isinstance(value, bytes | bytearray | memoryview):
        return "[binary content omitted]"
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class SampleAssociationView(BaseModel):
    id: UUID
    sample_sha256: str
    origin: str
    qualification: SampleQualification
    related_sample_sha256: list[str]
    version: int


class SampleDetailView(SampleAssociationView):
    changes: list[QualificationChangeView]


class OpinionBlockView(BaseModel):
    type: str
    text: str
    finding_refs: list[UUID]
    evidence_refs: list[UUID]
    qualification: str
    certainty: float


class OpinionDraftView(BaseModel):
    id: UUID
    version: int
    status: str
    content_sha256: str
    blocks: list[OpinionBlockView]
    unresolved_points: list[str]
    created_at: datetime


class BudgetDimensionView(BaseModel):
    limit: int | None
    consumed: int
    remaining: int | None


class BlockedDecisionView(BaseModel):
    code: str
    tool_name: str | None


class InvestigationView(BaseModel):
    id: UUID
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    status: AnalystInvestigationStatus
    execution_mode: InvestigationExecutionMode
    version: int
    stop_reason: StopReason | None
    agenda: list[AgendaItemView]
    hypotheses: list[HypothesisView]
    findings: list[FindingView]
    observations: list[ObservationView]
    sample_associations: list[SampleAssociationView]
    opinion_draft: OpinionDraftView | None
    budget: dict[str, BudgetDimensionView]
    blocked_decisions: list[BlockedDecisionView]
    available_tools: list[str]
    unavailable_tools: list[str]
    envelope_approved: bool
    allowed_providers: list[str]
    acquire_hits_allowed: bool


class InvestigationActionResponse(BaseModel):
    investigation: InvestigationView
    job_id: UUID | None
    idempotent_replay: bool


class UserRequestResponse(BaseModel):
    investigation: InvestigationView
    request_id: UUID
    job_id: UUID | None
    idempotent_replay: bool


class SampleQualificationResponse(BaseModel):
    association: SampleDetailView
    idempotent_replay: bool


class InvestigationSummaryView(BaseModel):
    id: UUID
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    status: AnalystInvestigationStatus
    version: int
    stop_reason: StopReason | None
    updated_at: datetime


class InvestigationOperatorStatusResponse(BaseModel):
    """Read-only operator projection; error codes use InvestigationOperatorErrorCode."""

    investigation_id: UUID
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    base_publication_artifact_id: UUID | None
    base_publication_sha256: str | None
    investigation_status: str
    execution_mode: InvestigationExecutionMode
    investigation_version: int
    working_state_version: int
    working_state_sha256: str
    deduplicated_findings: int
    job_generation: int
    active_job_ids: list[UUID]
    active_job_statuses: list[str]
    turn_index: int
    stop_reason: StopReason | None
    pending_context_snapshot_id: UUID | None
    pending_context_sha256: str | None
    last_context_sha256: str | None
    last_decision_sha256: str | None
    bridge_conversation_id: UUID
    bridge_conversation_id_scope: str
    bridge_external_target: dict[str, Any]
    last_error: dict[str, Any] | None
    last_operation: dict[str, Any] | None
    invocations: list[dict[str, Any]]
    ambiguous_invocations: list[dict[str, Any]]
    invocations_requiring_reconciliation: list[dict[str, Any]]
    evidence_checkpoint: dict[str, Any]
    opinion_checkpoint: dict[str, Any] | None
    g2: dict[str, Any]
    budget: dict[str, Any]
    budget_reservations: list[dict[str, Any]]
    dependencies: dict[str, list[str]]
    tool_inventory: dict[str, list[str]]
    advisor_guidance: AdvisorGuidanceV1
    next_idempotent_action: dict[str, Any]


@router.post(
    "/subjects/{subject_id}/investigations",
    response_model=InvestigationActionResponse,
)
async def start_investigation(
    subject_id: UUID,
    payload: InvestigationStartRequest,
    request: Request,
) -> InvestigationActionResponse:
    uow_factory = _uow_factory(request)
    composition_factory = _composition_factory(request)
    identity = await _identity(request)
    extraction_artifacts, extraction_items = await _ensure_handoff_if_missing(
        request, subject_id, payload
    )
    async with uow_factory() as uow:
        run = await uow.production_runs.get(payload.production_run_id)
        artifact = await uow.production_artifacts.get(payload.synthesis_artifact_id)
        if run is None or run.subject_id != subject_id:
            raise _http_error(404, "production_run_not_found")
        if artifact is None:
            raise _http_error(404, "synthesis_artifact_not_found")
        _validate_synthesis_link(subject_id, payload, run, artifact)
        investigation = await uow.analyst_investigations.get_for_run(run.id)
        if investigation is None or investigation.synthesis_artifact_id != artifact.id:
            raise _http_error(409, "investigation_handoff_missing")
        try:
            snapshot = await composition_factory.article_snapshots.resolve(artifact.id)
        except (ValueError, KeyError) as exc:
            raise _http_error(409, "synthesis_not_verified") from exc
        if (
            snapshot.subject_id != subject_id
            or snapshot.production_run_id != run.id
            or snapshot.synthesis_sha256 != payload.synthesis_sha256
        ):
            raise _http_error(409, "synthesis_identity_mismatch")

        composition = composition_factory.build(uow, investigation.id)
        envelope = await uow.investigation_envelopes.get(investigation.id)
        try:
            state = await composition.service.start(
                run=run,
                synthesis=artifact,
                extraction_artifacts=extraction_artifacts,
                extraction_items=extraction_items,
                policy=AnalystHandoffPolicy(
                    tlp=snapshot.tlp,
                    do_not_submit=snapshot.do_not_submit,
                    external_llm_allowed=snapshot.external_llm_allowed,
                ),
                envelope=envelope,
                execution_mode=payload.execution_mode,
                uow=uow,
            )
            job, replay = await _submit_run_job(
                request,
                uow,
                investigation.id,
                generation=1,
                request_key_sha256=_hash_key(
                    f"start:{run.id}:{artifact.id}:{payload.synthesis_sha256}"
                ),
                actor_id=identity.actor_id,
            )
            view = await _investigation_view(
                uow,
                investigation.id,
                state=state,
                composition=composition,
            )
            await uow.commit()
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "investigation_version_conflict") from exc
        except ValueError as exc:
            raise _http_error(409, "investigation_start_conflict", str(exc)) from exc
    await _dispatch(request, job)
    return InvestigationActionResponse(
        investigation=view,
        job_id=job.id,
        idempotent_replay=replay,
    )


async def _ensure_handoff_if_missing(
    request: Request,
    subject_id: UUID,
    payload: InvestigationStartRequest,
) -> tuple[tuple[ProductionArtifact, ...], tuple[Any, ...]]:
    """Create the canonical post-synthesis handoff before resolving article context.

    The article snapshot adapter intentionally requires an existing input pack. The
    HTTP start route is also the entry point that may create that pack, so this
    model-free bootstrap runs in its own committed transaction first.
    """
    uow_factory = _uow_factory(request)
    factory = _composition_factory(request)
    async with uow_factory() as uow:
        run = await uow.production_runs.get(payload.production_run_id)
        artifact = await uow.production_artifacts.get(payload.synthesis_artifact_id)
        if run is None or run.subject_id != subject_id:
            raise _http_error(404, "production_run_not_found")
        if artifact is None:
            raise _http_error(404, "synthesis_artifact_not_found")
        _validate_synthesis_link(subject_id, payload, run, artifact)
        existing = await uow.analyst_investigations.get_for_run(run.id)
        if existing is not None:
            if existing.synthesis_artifact_id != artifact.id:
                raise _http_error(409, "investigation_handoff_mismatch")
            return await current_verified_extraction_inputs(
                uow=uow,
                run=run,
                artifact_store=factory.artifact_store,
            )
        if artifact.canonical_blob_id is None:
            raise _http_error(409, "synthesis_not_verified")
        try:
            synthesis_payload = await factory.artifact_store.read_json(artifact.canonical_blob_id)
        except (KeyError, ValueError) as exc:
            raise _http_error(409, "synthesis_not_verified") from exc
        synthesis_sha256 = hashlib.sha256(
            factory.artifact_store.canonical_json_bytes(synthesis_payload)
        ).hexdigest()
        if (
            synthesis_payload.get("subject_id") != str(subject_id)
            or synthesis_sha256 != payload.synthesis_sha256
        ):
            raise _http_error(409, "synthesis_identity_mismatch")
        input_snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        if input_snapshot is None:
            raise _http_error(409, "production_input_snapshot_missing")
        policy = analyst_handoff_policy_from_sources(input_snapshot.core_sources)
        if input_snapshot.subject_tlp.value.upper() == "RED":
            policy = AnalystHandoffPolicy(
                tlp=input_snapshot.subject_tlp.value,
                do_not_submit=True,
                external_llm_allowed=False,
            )
        else:
            policy = AnalystHandoffPolicy(
                tlp=input_snapshot.subject_tlp.value,
                do_not_submit=policy.do_not_submit,
                external_llm_allowed=policy.external_llm_allowed
                and bool(input_snapshot.core_sources),
            )
        extraction_artifacts, extraction_items = await current_verified_extraction_inputs(
            uow=uow,
            run=run,
            artifact_store=factory.artifact_store,
        )
        handoff = AnalystPostSynthesisService(
            uow_factory,
            factory.artifact_store,
            loop_budget_factory=lambda: loop_budget_from_settings(factory.settings),
        )
        try:
            await handoff.ensure_for_verified_synthesis(
                run=run,
                synthesis=artifact,
                extraction_artifacts=extraction_artifacts,
                extraction_items=extraction_items,
                policy=policy,
                uow=uow,
            )
            await uow.commit()
        except ValueError as exc:
            raise _http_error(409, "investigation_handoff_conflict", str(exc)) from exc
        return extraction_artifacts, extraction_items


@router.get("/investigations/{investigation_id}", response_model=InvestigationView)
async def get_investigation(investigation_id: UUID, request: Request) -> InvestigationView:
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id)
        return await _investigation_view(
            uow,
            investigation_id,
            composition=composition,
        )


@router.get(
    "/investigations/{investigation_id}/experiments",
    response_model=InvestigationExperimentTimelineResponseV1,
)
async def get_investigation_experiments(
    investigation_id: UUID, request: Request
) -> InvestigationExperimentTimelineResponseV1:
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        repository = getattr(uow, "investigation_experiments", None)
        if repository is None:
            raise _http_error(503, "investigation_experiments_unavailable")
        timeline = await build_investigation_experiment_timeline(repository, investigation_id)
    return InvestigationExperimentTimelineResponseV1.model_validate(
        timeline.to_payload(), strict=False
    )


@router.get(
    "/investigations/{investigation_id}/pivots",
    response_model=PivotFrontierResponseV1,
)
async def get_investigation_pivot_frontier(
    investigation_id: UUID,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    cursor: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
) -> PivotFrontierResponseV1:
    """Read the deterministic pivot graph derived from the article and stored evidence."""
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        try:
            snapshot = await _composition_factory(request).article_snapshots.resolve(
                investigation.synthesis_artifact_id
            )
        except (KeyError, ValueError) as exc:
            raise _http_error(409, "pivot_frontier_article_unavailable") from exc
        observations = tuple(
            await uow.evidence_observations.list_for_investigation(investigation_id)
        )
        invocations = tuple(await uow.tool_invocations.list_for_investigation(investigation_id))
        envelope = await uow.investigation_envelopes.get(investigation_id)
        payloads = await _pivot_observation_payloads(request, uow, observations)
        try:
            frontier = build_pivot_frontier(
                investigation_id=str(investigation_id),
                snapshot=snapshot,
                observations=observations,
                observation_payloads=payloads,
                invocations=invocations,
                envelope=envelope,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise _http_error(422, "pivot_frontier_cursor_invalid") from exc
    payload = frontier_to_dict(frontier)
    payload["investigation_id"] = investigation_id
    payload["nodes"] = payload.pop("records")
    return PivotFrontierResponseV1.model_validate(payload, strict=False)


@router.get(
    "/investigations/{investigation_id}/operator/context",
    response_model=InvestigationOperatorContextV1 | InvestigationOperatorContextSummaryV1,
)
async def get_investigation_operator_context(
    investigation_id: UUID,
    request: Request,
    view: Literal["full", "summary"] = Query(default="full"),
    expected_version: int | None = Query(default=None, ge=1),
    expected_sha256: str | None = Query(default=None, pattern=_SHA256),
    since_version: int | None = Query(default=None, ge=1),
) -> InvestigationOperatorContextV1 | InvestigationOperatorContextSummaryV1:
    await _require_operator_client(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id, operator_mode=True)
        try:
            if view == "summary":
                return await composition.operator_tool_invocations.get_context_summary(
                    expected_version=expected_version,
                    expected_sha256=expected_sha256,
                    since_version=since_version,
                )
            return await composition.operator_tool_invocations.get_context(
                expected_version=expected_version,
                expected_sha256=expected_sha256,
            )
        except OperatorToolInvocationError as exc:
            if exc.code == "operator_context_too_large":
                _logger.error(
                    "operator_context_too_large investigation_id=%s dominant_sections=%s",
                    investigation_id,
                    exc.details.get("dominant_sections", [])[:8],
                )
            raise _http_error(exc.status_code, exc.code, details=exc.details) from exc
        except InvestigationContextTooLarge as exc:
            _logger.error(
                "operator_context_too_large investigation_id=%s dominant_sections=%s",
                investigation_id,
                exc.dominant_sections[:8],
            )
            raise _http_error(
                413,
                "operator_context_too_large",
                details={"dominant_sections": list(exc.dominant_sections[:8])},
            ) from exc
        except ValidationError as exc:
            errors = _bounded_validation_errors(exc)
            _logger.error(
                "operator_context_validation_failed investigation_id=%s errors=%s",
                investigation_id,
                errors,
            )
            raise _http_error(503, "operator_context_invalid") from exc
        except (KeyError, ValueError) as exc:
            _logger.warning(
                "operator_context_unavailable investigation_id=%s error_type=%s error_code=%s",
                investigation_id,
                type(exc).__name__,
                str(getattr(exc, "code", "invalid_value"))[:96],
            )
            raise _http_error(503, "operator_context_unavailable") from exc


@router.get(
    "/investigations/{investigation_id}/operator/context/section/{section}",
    response_model=OperatorContextSectionPageV1 | OperatorContextItemPageV1,
)
async def get_investigation_operator_context_section(
    investigation_id: UUID,
    section: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=20),
    expected_version: int | None = Query(default=None, ge=1),
    expected_sha256: str | None = Query(default=None, pattern=_SHA256),
) -> OperatorContextSectionPageV1 | OperatorContextItemPageV1:
    await _require_article_claim_reader(request, section)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id, operator_mode=True)
        try:
            return await composition.operator_tool_invocations.get_context_section(
                section,
                offset=offset,
                limit=limit,
                expected_version=expected_version,
                expected_sha256=expected_sha256,
            )
        except OperatorToolInvocationError as exc:
            raise _http_error(exc.status_code, exc.code, details=exc.details) from exc
        except ValidationError as exc:
            errors = _bounded_validation_errors(exc)
            _logger.error(
                "operator_context_section_validation_failed "
                "investigation_id=%s section=%s errors=%s",
                investigation_id,
                section[:64],
                errors,
            )
            raise _http_error(503, "operator_context_section_invalid") from exc
        except (KeyError, ValueError) as exc:
            _logger.warning(
                "operator_context_section_unavailable investigation_id=%s section=%s error_type=%s",
                investigation_id,
                section[:64],
                type(exc).__name__,
            )
            raise _http_error(503, "operator_context_section_unavailable") from exc


@router.post(
    "/investigations/{investigation_id}/operator/tools/{tool_name}",
    response_model=OperatorToolInvocationApiResponseV1,
)
async def invoke_investigation_operator_tool(
    investigation_id: UUID,
    tool_name: str,
    payload: OperatorToolInvocationRequestV1,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> OperatorToolInvocationApiResponseV1:
    await _require_operator_client(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id, operator_mode=True)
        try:
            result = await composition.operator_tool_invocations.invoke(
                tool_name=tool_name,
                schema_version=payload.schema_version,
                arguments=payload.arguments,
                purpose=payload.purpose,
                expected_version=payload.expected_version,
                idempotency_key=idempotency_key,
            )
        except OperatorToolInvocationError as exc:
            raise _http_error(exc.status_code, exc.code) from exc
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "stale_working_state") from exc
        advisor_output = None
        advisor_output_not_replayed = False
        if tool_name == "advisor.consult" and composition.advisor_tool is not None:
            captured = composition.advisor_tool.take_result(result.invocation.operation_key)
            if captured is not None:
                advisor_output = dict(captured.output)
            elif (
                result.idempotent_replay
                and result.invocation.status is ToolInvocationStatus.SUCCEEDED
            ):
                advisor_output_not_replayed = True
    return OperatorToolInvocationApiResponseV1(
        investigation_id=result.investigation_id,
        working_state_version=result.working_state_version,
        idempotent_replay=result.idempotent_replay,
        invocation=OperatorToolInvocationViewV1(
            id=result.invocation.id,
            tool_name=result.invocation.tool_name,
            schema_version=result.invocation.schema_version,
            status=result.invocation.status,
            version=result.invocation.version,
            response_refs=result.invocation.response_refs,
            error_code=result.error_code,
        ),
        job_id=result.job_id,
        advisor_output=advisor_output,
        advisor_output_not_replayed=advisor_output_not_replayed,
        output=dict(result.output),
    )


@router.get(
    "/investigations/{investigation_id}/operator/observations/{observation_id}",
    response_model=OperatorObservationPayloadResponse,
)
async def get_investigation_operator_observation(
    investigation_id: UUID,
    observation_id: UUID,
    request: Request,
    offset: int = Query(default=0, ge=0),
    length: int = Query(default=16 * 1024, ge=1, le=32 * 1024),
) -> OperatorObservationPayloadResponse:
    await _require_operator_client(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        observation = await uow.evidence_observations.get(observation_id)
        if observation is None or observation.investigation_id != investigation_id:
            raise _http_error(404, "operator_observation_not_found")
        blob = await uow.blobs.get(observation.blob_id)
        descriptor = getattr(blob, "descriptor", None)
        if (
            descriptor is None
            or descriptor.mime_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise _http_error(415, "operator_observation_not_json")
        if descriptor.logical_bucket in {"samples-quarantine", "network-quarantine"}:
            raise _http_error(415, "operator_observation_binary_forbidden")
        try:
            raw = await _artifact_store(request).read_bytes(
                observation.blob_id, max_bytes=8 * 1024 * 1024
            )
        except (ValueError, KeyError) as exc:
            raise _http_error(413, "operator_observation_too_large") from exc
        if hashlib.sha256(raw).hexdigest() != observation.sha256:
            raise _http_error(409, "operator_observation_integrity_failed")
        try:
            canonical = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _http_error(422, "operator_observation_invalid_json") from exc
        if not isinstance(canonical, dict):
            raise _http_error(422, "operator_observation_invalid_payload")
        allow_confined_text_chunks = (
            getattr(observation, "source", None) == "AutoWork local analysis"
            and getattr(observation, "object_type", None) == "file"
            and canonical.get("sample_sha256") == getattr(observation, "object_id", None)
        )
        try:
            sanitized = _sanitize_observation_payload(
                canonical, allow_confined_text_chunks=allow_confined_text_chunks
            )
        except _SensitiveObservationTextError as exc:
            raise _http_error(422, "operator_observation_sensitive_text") from exc
        payload_json = json.dumps(
            sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(payload_json.encode("utf-8")) > 8 * 1024 * 1024:
            raise _http_error(413, "operator_observation_too_large")
        page = payload_json[offset : offset + length]
        next_offset = offset + len(page) if offset + len(page) < len(payload_json) else None
        return OperatorObservationPayloadResponse(
            observation_id=observation.id,
            sha256=observation.sha256,
            offset=offset,
            length=len(page),
            total_chars=len(payload_json),
            next_offset=next_offset,
            payload=page,
            untrusted_data=True,
        )


@router.get(
    "/investigations/{investigation_id}/operator-status",
    response_model=InvestigationOperatorStatusResponse,
)
async def get_investigation_operator_status(
    investigation_id: UUID,
    request: Request,
) -> InvestigationOperatorStatusResponse:
    """Read durable checkpoints without changing versions.

    Stable error codes are ``investigation_not_found`` (404),
    ``investigation_not_started`` (409), and
    ``investigation_operator_status_unavailable`` (503). A revoked envelope
    exposes ``g2.revoked`` and its next human action; unsupported legacy
    reapproval uses ``envelope_revoked`` (409).
    """
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, InvestigationOperatorErrorCode.INVESTIGATION_NOT_FOUND.value)
        state = await uow.investigation_states.get(investigation_id)
        if state is None:
            raise _http_error(409, InvestigationOperatorErrorCode.INVESTIGATION_STATE_MISSING.value)
        composition_factory = _composition_factory(request)
        composition = composition_factory.build(
            uow,
            investigation_id,
            operator_mode=state.execution_mode is InvestigationExecutionMode.OPERATOR_LED,
        )
        try:
            snapshot = await composition.tool_dependencies.article_snapshots.resolve(
                investigation.synthesis_artifact_id
            )
        except (KeyError, ValueError) as exc:
            raise _http_error(503, InvestigationOperatorErrorCode.STATUS_UNAVAILABLE.value) from exc
        envelope = await uow.investigation_envelopes.get(investigation_id)
        decisions = tuple(await uow.analyst_decisions.list_for_investigation(investigation_id))
        authorization = g2_envelope_authorization_state(envelope, decisions)
        invocations = tuple(await uow.tool_invocations.list_for_investigation(investigation_id))
        observations = tuple(
            await uow.evidence_observations.list_for_investigation(investigation_id)
        )
        tool_inventory = await _operator_status_tool_inventory(
            state=state,
            composition=composition,
            envelope=envelope,
            invocations=invocations,
            observations=observations,
            external_llm_allowed=InvestigationService.external_llm_allowed(snapshot, envelope),
        )
        if tool_inventory is not None:
            available_tool_names, unavailable_tool_diagnostics = tool_inventory
        else:
            # Compatibility for lightweight route fakes; production compositions always expose
            # the registry and use the shared availability projection above.
            available_tool_names = tuple(getattr(composition, "available_tools", ()))
            unavailable_tool_diagnostics = _unavailable_tool_diagnostics(request, composition)
        advisor_settings = composition_factory.settings
        artifact_store = getattr(
            getattr(composition, "tool_dependencies", None), "artifact_store", None
        )
        advisor_guidance = await build_advisor_guidance(
            invocations=invocations,
            observations=observations,
            evidence_summaries=(
                CanonicalEvidenceSummaryPort(artifact_store) if artifact_store is not None else None
            ),
            available_tools=available_tool_names,
            checkpoint_tool_threshold=getattr(
                advisor_settings, "operator_advisor_checkpoint_tool_threshold", 12
            ),
            advisor_turn_limit=getattr(
                advisor_settings, "operator_advisor_max_turns_per_investigation", 8
            ),
            enabled=state.execution_mode is InvestigationExecutionMode.OPERATOR_LED,
        )
        opinion = await uow.analyst_opinions.get(uuid5(_OPINION_NAMESPACE, str(investigation_id)))
        jobs = tuple(
            await uow.jobs.list_for_aggregate(
                INVESTIGATION_RUN_AGGREGATE_TYPE,
                investigation_id,
                kind=INVESTIGATION_RUN_JOB_KIND,
            )
        )
        pending_bridge_turn = None
        if state.pending_context_sha256 is not None:
            attempt = investigation_bridge_attempt(state.recent_actions, state.turn_index)
            key = make_investigation_turn_idempotency_key(
                investigation_id=investigation_id,
                turn_index=state.turn_index,
                context_sha256=state.pending_context_sha256,
                attempt=attempt,
            )
            pending_bridge_turn = await uow.model_conversation_turns.get_by_idempotency_key(key)
            if pending_bridge_turn is None:
                abandoned_turn_id = investigation_abandoned_turn_id(
                    state.recent_actions, state.turn_index, attempt
                )
                if abandoned_turn_id is None:
                    abandoned_turn_id = investigation_orphaned_turn_id(
                        state.recent_actions, state.turn_index, attempt
                    )
                if abandoned_turn_id is not None:
                    pending_bridge_turn = await uow.model_conversation_turns.get(abandoned_turn_id)
        view = build_investigation_operator_view(
            investigation=investigation,
            state=state,
            synthesis_sha256=snapshot.synthesis_sha256,
            envelope=envelope,
            g2_revoked=authorization.revoked,
            g2_reapproval_supported=(
                authorization.revoked and authorization.authorization_ceiling is not None
            ),
            invocations=invocations,
            observations=observations,
            opinion=opinion,
            jobs=jobs,
            available_tools=available_tool_names,
            unavailable_tools=unavailable_tool_diagnostics,
            advisor_guidance=advisor_guidance,
            pending_bridge_turn_id=(pending_bridge_turn.id if pending_bridge_turn else None),
            pending_bridge_turn_status=(
                pending_bridge_turn.status.value if pending_bridge_turn else None
            ),
            pending_bridge_turn_error_code=(
                pending_bridge_turn.error_code if pending_bridge_turn else None
            ),
            pending_bridge_turn_key=(
                pending_bridge_turn.idempotency_key if pending_bridge_turn else None
            ),
        )
        return InvestigationOperatorStatusResponse.model_validate(view.payload)


@router.get("/investigations/{investigation_id}/envelope", response_model=EnvelopeView)
async def get_investigation_envelope(
    investigation_id: UUID,
    request: Request,
) -> EnvelopeView:
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id, operator_mode=True)
        snapshot = await composition.tool_dependencies.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        current = await uow.investigation_envelopes.get(investigation_id)
        settings = _composition_factory(request).settings
        if current is None:
            budget = _configured_budget(settings)
            allowed_providers: tuple[str, ...] = ()
            acquire_hits_allowed = False
            disclosure = _source_disclosure(snapshot)
            version = 0
            decision_id = None
            actor_id = None
            approved_at = None
        else:
            budget = current.budget
            allowed_providers = current.allowed_providers
            acquire_hits_allowed = current.acquire_hits_allowed
            disclosure = current.disclosure
            version = current.version
            decision_id = current.approval_decision_id
            actor_id = current.approval_actor_id
            approved_at = current.approved_at
        return _envelope_view(
            investigation_id=investigation_id,
            budget=budget,
            approved=current is not None,
            version=version,
            allowed_providers=allowed_providers,
            available_providers=_available_providers(composition.available_tools),
            acquire_hits_allowed=acquire_hits_allowed,
            disclosure=disclosure,
            approval_decision_id=decision_id,
            approval_actor_id=actor_id,
            approved_at=approved_at,
        )


@router.post("/investigations/{investigation_id}/envelope/approve", response_model=EnvelopeView)
async def approve_investigation_envelope(
    investigation_id: UUID,
    payload: EnvelopeDecisionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> EnvelopeView:
    identity = await _identity(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id, operator_mode=True)
        snapshot = await composition.tool_dependencies.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        available = _available_providers(composition.available_tools)
        budget = _budget_from_request(
            payload,
            defaults=_configured_budget(_composition_factory(request).settings),
        )
        providers = _requested_providers(payload.allowed_providers, available)
        disclosure = _disclosure_from_request(payload.disclosure, snapshot)
    proposal = InvestigationEnvelopeProposal(
        investigation_id=investigation_id,
        allowed_providers=providers,
        budget=budget,
        acquire_hits_allowed=bool(payload.acquire_hits_allowed),
        disclosure=disclosure,
    )
    try:
        await InvestigationEnvelopeService(_uow_factory(request)).approve(
            proposal,
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
            expected_version=payload.expected_version,
        )
    except EnvelopeNotFoundError as exc:
        raise _http_error(404, "investigation_not_found") from exc
    except (EnvelopeVersionConflictError, EnvelopeDecisionConflictError) as exc:
        raise _http_error(409, "envelope_conflict", str(exc)) from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_envelope", str(exc)) from exc
    return await _current_envelope_view(request, investigation_id)


@router.post("/investigations/{investigation_id}/envelope/extend", response_model=EnvelopeView)
async def extend_investigation_envelope(
    investigation_id: UUID,
    payload: EnvelopeDecisionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> EnvelopeView:
    identity = await _identity(request)
    service = InvestigationEnvelopeService(_uow_factory(request))
    async with _uow_factory(request)() as uow:
        current = await uow.investigation_envelopes.get(investigation_id)
        if current is None:
            raise _http_error(404, "envelope_not_found")
        if payload.expected_version is None:
            raise _http_error(422, "expected_version_required")
        decisions = tuple(await uow.analyst_decisions.list_for_investigation(investigation_id))
        authorization = g2_envelope_authorization_state(current, decisions)
        if authorization.revoked and authorization.authorization_ceiling is None:
            raise _http_error(
                409,
                InvestigationOperatorErrorCode.ENVELOPE_REVOKED.value,
                "The revoked envelope has no retained authorization bounds; "
                "create a new investigation.",
            )
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id, operator_mode=True)
        snapshot = await composition.tool_dependencies.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        providers = _requested_providers(
            payload.allowed_providers or current.allowed_providers,
            _available_providers(composition.available_tools),
        )
        budget = _budget_from_request(
            payload,
            defaults=current.budget,
            acquire_default=current.acquire_hits_allowed,
        )
        acquire = (
            current.acquire_hits_allowed
            if payload.acquire_hits_allowed is None
            else payload.acquire_hits_allowed
        )
        disclosure = _disclosure_from_request(
            payload.disclosure,
            snapshot,
            current=current.disclosure,
            ceiling=(
                authorization.authorization_ceiling.disclosure
                if authorization.authorization_ceiling is not None
                else current.disclosure
            ),
        )
    try:
        await service.extend(
            investigation_id,
            expected_version=payload.expected_version,
            allowed_providers=providers,
            budget=budget,
            acquire_hits_allowed=acquire,
            disclosure=disclosure,
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
        )
    except EnvelopeNotFoundError as exc:
        raise _http_error(404, "envelope_not_found") from exc
    except EnvelopeRevokedError as exc:
        raise _http_error(
            409, InvestigationOperatorErrorCode.ENVELOPE_REVOKED.value, str(exc)
        ) from exc
    except (EnvelopeVersionConflictError, EnvelopeDecisionConflictError) as exc:
        raise _http_error(409, "envelope_conflict", str(exc)) from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_envelope", str(exc)) from exc
    return await _current_envelope_view(request, investigation_id)


@router.post("/investigations/{investigation_id}/envelope/revoke", response_model=EnvelopeView)
async def revoke_investigation_envelope(
    investigation_id: UUID,
    payload: EnvelopeRevokeRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> EnvelopeView:
    identity = await _identity(request)
    try:
        await InvestigationEnvelopeService(_uow_factory(request)).revoke(
            investigation_id,
            expected_version=payload.expected_version,
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
        )
    except EnvelopeNotFoundError as exc:
        raise _http_error(404, "envelope_not_found") from exc
    except (EnvelopeVersionConflictError, EnvelopeDecisionConflictError) as exc:
        raise _http_error(409, "envelope_conflict", str(exc)) from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_envelope", str(exc)) from exc
    return await _current_envelope_view(request, investigation_id)


@router.get(
    "/investigations/{investigation_id}/samples",
    response_model=list[SampleDetailView],
)
async def list_investigation_samples(
    investigation_id: UUID,
    request: Request,
) -> list[SampleDetailView]:
    async with _uow_factory(request)() as uow:
        if await uow.analyst_investigations.get(investigation_id) is None:
            raise _http_error(404, "investigation_not_found")
        associations = await uow.sample_associations.list_for_investigation(investigation_id)
        return [_sample_detail(item) for item in associations]


@router.get("/investigations/{investigation_id}/sample-comparisons")
async def list_investigation_sample_comparisons(
    investigation_id: UUID,
    request: Request,
    limit: int = Query(default=25, ge=1, le=50),
) -> dict[str, Any]:
    artifact_store = _artifact_store(request)
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        summaries: list[dict[str, Any]] = []
        omitted_comparisons: list[dict[str, Any]] = []
        omitted_comparisons_truncated = False
        seen: set[UUID] = set()
        for observation in observations:
            if observation.object_type != "family_feature_comparison":
                continue
            blob = await uow.blobs.get_by_address(_SIMILARITY_BUCKET, observation.object_id)
            if blob is None or blob.id in seen or blob.descriptor.sha256 != observation.sha256:
                continue
            try:
                raw = await artifact_store.read_json(blob.id)
            except BlobReadLimitExceededError as exc:
                seen.add(blob.id)
                _logger.warning(
                    "Sample comparison omitted because its canonical blob exceeds the read limit",
                    extra={
                        "event": "sample_comparison_omitted",
                        "observation_id": str(observation.id),
                        "size_bytes": exc.size_bytes,
                        "reason": "exceeds_read_limit",
                    },
                )
                if len(omitted_comparisons) < limit:
                    omitted_comparisons.append(
                        {
                            "observation_id": str(observation.id),
                            "size_bytes": exc.size_bytes,
                            "reason": "exceeds_read_limit",
                        }
                    )
                else:
                    omitted_comparisons_truncated = True
                continue
            if raw.get("investigation_id") != str(investigation_id):
                continue
            seen.add(blob.id)
            summaries.append(_comparison_summary(raw, blob.id, blob.descriptor.sha256))
            if len(summaries) > limit:
                break
    return {
        "investigation_id": investigation_id,
        "comparisons": summaries[:limit],
        "truncated": len(summaries) > limit,
        "omitted_comparisons": omitted_comparisons,
        "omitted_comparisons_truncated": omitted_comparisons_truncated,
    }


@router.get("/investigations/{investigation_id}/sample-comparisons/{blob_id}")
async def get_investigation_sample_comparison(
    investigation_id: UUID,
    blob_id: UUID,
    request: Request,
    feature_limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    artifact_store = _artifact_store(request)
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        blob = await _require_evidence_blob(
            uow,
            investigation_id=investigation_id,
            blob_id=blob_id,
            logical_bucket=_SIMILARITY_BUCKET,
            object_type="family_feature_comparison",
        )
    raw = await artifact_store.read_json(blob.id)
    if raw.get("investigation_id") != str(investigation_id):
        raise _http_error(404, "sample_comparison_not_found")
    return _bounded_comparison(raw, blob.id, blob.descriptor.sha256, feature_limit=feature_limit)


@router.get("/investigations/{investigation_id}/hypotheses")
async def list_investigation_hypotheses(
    investigation_id: UUID,
    request: Request,
) -> dict[str, Any]:
    async with _uow_factory(request)() as uow:
        investigation, state = await _require_working_state(uow, investigation_id)
        all_findings = await uow.investigation_findings.list_for_investigation(investigation_id)
        superseded_ids = {item.supersedes for item in all_findings if item.supersedes is not None}
        findings_by_id = {item.id: item for item in all_findings if item.id not in superseded_ids}
        observations_by_id = {
            item.id: item
            for item in await uow.evidence_observations.list_for_investigation(investigation_id)
        }
        hypotheses: list[dict[str, Any]] = []
        for hypothesis in state.hypotheses[:64]:
            finding_ids = tuple(ref for ref in hypothesis.finding_refs if ref in findings_by_id)[
                :64
            ]
            linked_observation_refs = tuple(
                dict.fromkeys(
                    (
                        *hypothesis.evidence_refs,
                        *(
                            observation_ref
                            for finding_id in finding_ids
                            for observation_ref in findings_by_id[finding_id].observation_refs
                        ),
                    )
                )
            )
            observation_ids = tuple(
                ref for ref in linked_observation_refs if ref in observations_by_id
            )[:64]
            linked_findings = [findings_by_id[ref] for ref in finding_ids]
            linked_observations = [observations_by_id[ref] for ref in observation_ids]
            contradictions = [
                _finding_evidence_view(item)
                for item in linked_findings
                if item.assessment is FindingAssessment.REFUTED
            ]
            hypotheses.append(
                {
                    "id": hypothesis.id,
                    "statement": hypothesis.statement,
                    "status": hypothesis.status.value,
                    "findings": [_finding_evidence_view(item) for item in linked_findings],
                    "observations": [
                        _observation_evidence_view(item) for item in linked_observations
                    ],
                    "contradictions": contradictions,
                }
            )
    return {"investigation_id": investigation.id, "hypotheses": hypotheses}


@router.get("/investigations/{investigation_id}/protocol-models")
async def list_investigation_protocol_models(
    investigation_id: UUID,
    request: Request,
    limit: int = Query(default=25, ge=1, le=50),
) -> dict[str, Any]:
    artifact_store = _artifact_store(request)
    async with _uow_factory(request)() as uow:
        await _require_investigation(uow, investigation_id)
        observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        models: list[dict[str, Any]] = []
        seen: set[UUID] = set()
        for observation in observations:
            if observation.object_type != "protocol_model":
                continue
            blob = await uow.blobs.get_by_address(_PROTOCOL_MODEL_BUCKET, observation.object_id)
            if blob is None or blob.id in seen or blob.descriptor.sha256 != observation.object_id:
                continue
            model = ProtocolModelV1.from_dict(await artifact_store.read_json(blob.id))
            if model.investigation_id != investigation_id:
                continue
            seen.add(blob.id)
            models.append(_protocol_model_summary(model, blob.id, observation.object_id))
            if len(models) > limit:
                break
    return {
        "investigation_id": investigation_id,
        "models": models[:limit],
        "truncated": len(models) > limit,
    }


@router.get("/investigations/{investigation_id}/protocol-models/{blob_id}")
async def get_investigation_protocol_model(
    investigation_id: UUID,
    blob_id: UUID,
    request: Request,
) -> dict[str, Any]:
    model = await _read_protocol_model(request, investigation_id, blob_id)
    return _bounded_protocol_model(model, blob_id)


@router.get("/investigations/{investigation_id}/protocol-models/{blob_id}/diagrams")
async def get_investigation_protocol_diagrams(
    investigation_id: UUID,
    blob_id: UUID,
    request: Request,
) -> dict[str, Any]:
    model = await _read_protocol_model(request, investigation_id, blob_id)
    try:
        rendered = await compile_protocol_render_preview(
            render_protocol_model(model),
            compiler=D2DiagramCompiler(),
        )
    except (DiagramCompilationError, ProtocolDiagramProjectionError) as exc:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "protocol_diagram_rendering_unavailable",
        ) from exc
    return {
        "investigation_id": investigation_id,
        "model_blob_id": blob_id,
        "model_sha256": model.canonical_sha256,
        "diagrams": [
            {
                "kind": item.spec.kind.value,
                "status": item.spec.status.value,
                "title": item.spec.title,
                "limitations": list(item.spec.limitations[:32]),
                "asset_sha256": item.compiled.media_sha256,
                "svg": {
                    "mime_type": item.compiled.media_type,
                    "sha256": item.compiled.media_sha256,
                    "data_url": _data_url(item.compiled.media_type, item.compiled.media_bytes),
                },
                "png_preview": {
                    "mime_type": item.preview.media_type,
                    "sha256": item.preview.media_sha256,
                    "data_url": _data_url(item.preview.media_type, item.preview.media_bytes),
                },
            }
            for item in rendered
        ],
    }


@router.get("/investigations/{investigation_id}/retrohunt")
async def get_investigation_retrohunt(
    investigation_id: UUID,
    request: Request,
) -> dict[str, Any]:
    async with _uow_factory(request)() as uow:
        investigation = await _require_investigation(uow, investigation_id)
        consumed = await uow.retrohunt_reservations.count_for_subject(investigation.subject_id)
        reservations = await uow.retrohunt_reservations.list_for_subject(investigation.subject_id)
    jobs = [
        {
            "reservation_id": item.id,
            "slot": item.slot_number,
            "status": item.state.value,
            "version": item.version,
            "updated_at": item.updated_at,
            "has_provider_job": item.provider_job_id is not None,
        }
        for item in reservations
        if item.investigation_id == investigation_id
    ]
    return {
        "investigation_id": investigation_id,
        "subject_id": investigation.subject_id,
        "maximum_slots": RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT,
        "consumed_slots": consumed,
        "remaining_slots": max(0, RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT - consumed),
        "jobs": jobs,
    }


@router.post(
    "/investigations/{investigation_id}/samples/{association_id}/qualify",
    response_model=SampleQualificationResponse,
)
async def qualify_investigation_sample(
    investigation_id: UUID,
    association_id: UUID,
    payload: SampleQualificationRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> SampleQualificationResponse:
    identity = await _identity(request)
    async with _uow_factory(request)() as uow:
        if await uow.analyst_investigations.get(investigation_id) is None:
            raise _http_error(404, "investigation_not_found")
        association = await uow.sample_associations.get(association_id)
        if association is None or association.investigation_id != investigation_id:
            raise _http_error(404, "sample_association_not_found")
        if association.version != payload.expected_version:
            if association.qualification is not payload.qualification:
                raise _http_error(409, "stale_sample_association_version")
            return SampleQualificationResponse(
                association=_sample_detail(association), idempotent_replay=True
            )
    try:
        qualified = await SampleQualificationService(_uow_factory(request)).qualify(
            association_id,
            payload.qualification,
            InvestigationActor(
                actor_id=identity.actor_id,
                kind=QualificationActorKind.HUMAN,
            ),
            payload.reason,
            payload.evidence_refs,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
            expected_version=payload.expected_version,
        )
    except SampleAssociationNotFoundError as exc:
        raise _http_error(404, "sample_association_not_found") from exc
    except SampleQualificationPermissionError as exc:
        raise _http_error(403, "human_qualification_required", str(exc)) from exc
    except SampleQualificationEvidenceError as exc:
        raise _http_error(422, "invalid_qualification_evidence", str(exc)) from exc
    except VersionConflictError as exc:
        raise _http_error(409, "stale_sample_association_version") from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_qualification", str(exc)) from exc
    return SampleQualificationResponse(
        association=_sample_detail(qualified), idempotent_replay=False
    )


@router.post(
    "/investigations/{investigation_id}/requests",
    response_model=UserRequestResponse,
)
async def request_targeted_investigation(
    investigation_id: UUID,
    payload: InvestigationUserRequestBody,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> UserRequestResponse:
    key_hash = _hash_key(idempotency_key)
    target = payload.target.model_dump(mode="json") if payload.target else None
    if target and target["kind"] == "hash" and not re.fullmatch(_SHA256, target["value"]):
        raise _http_error(422, "invalid_request_target")
    request_fingerprint = hashlib.sha256(
        json.dumps(
            {"text": payload.text, "target": target},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    marker_prefix = f"api:user-request:{key_hash}:"
    request_id = uuid5(_USER_REQUEST_NAMESPACE, f"{investigation_id}:{key_hash}")
    marker = f"{marker_prefix}{request_fingerprint}:{request_id}"
    job: Job | None = None
    replay = False
    identity = await _identity(request)
    async with _uow_factory(request)() as uow:
        investigation, state = await _require_working_state(uow, investigation_id)
        previous = next(
            (item for item in state.recent_actions if item.startswith(marker_prefix)), None
        )
        if previous is not None:
            if previous != marker:
                raise _http_error(409, "idempotency_key_reused")
            replay = True
        else:
            if payload.expected_version is not None and state.version != payload.expected_version:
                raise _http_error(409, "stale_investigation_version")
            pending_request_actions = tuple(
                item for item in state.recent_actions if item.startswith(USER_REQUEST_ACTION_PREFIX)
            )
            if len(pending_request_actions) >= MAX_OPERATOR_REQUESTS:
                raise _http_error(409, "investigation_user_request_limit_reached")
            user_request = {
                "request_id": str(request_id),
                "text": payload.text,
                "target": target,
            }
            action = USER_REQUEST_ACTION_PREFIX + json.dumps(
                user_request,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            pending_bytes = sum(len(item.encode("utf-8")) for item in pending_request_actions)
            if pending_bytes + len(action.encode("utf-8")) > MAX_OPERATOR_REQUEST_BYTES:
                raise _http_error(409, "investigation_user_request_limit_reached")
            protected_request_ids = {
                str(request_id),
                *(
                    parsed_request_id
                    for item in pending_request_actions
                    if (parsed_request_id := _user_request_id(item)) is not None
                ),
            }
            all_actions = (*state.recent_actions, action, marker)
            protected_actions = tuple(
                item
                for item in all_actions
                if item.startswith(USER_REQUEST_ACTION_PREFIX)
                or (
                    item.startswith(_USER_REQUEST_MARKER_PREFIX)
                    and item.rsplit(":", 1)[-1] in protected_request_ids
                )
            )
            protected_set = set(protected_actions)
            history_actions = tuple(item for item in all_actions if item not in protected_set)
            history_limit = max(0, 32 - len(protected_actions))
            if history_limit:
                history_actions = history_actions[-history_limit:]
            else:
                history_actions = ()
            recent_actions = (*history_actions, *protected_actions)
            updated = replace(
                state,
                version=state.version + 1,
                recent_actions=recent_actions,
            )
            try:
                await uow.investigation_states.save_if_version(
                    updated, expected_version=state.version
                )
            except (VersionConflictError, InvestigationStateVersionConflict) as exc:
                raise _http_error(409, "stale_investigation_version") from exc
            state = updated
        jobs = await uow.jobs.list_for_aggregate(
            INVESTIGATION_RUN_AGGREGATE_TYPE,
            investigation_id,
            kind=INVESTIGATION_RUN_JOB_KIND,
        )
        active = _active_job(jobs)
        if active is not None:
            job = active
        elif replay:
            job = next(
                (
                    item
                    for item in jobs
                    if item.input_parameters.get("request_key_sha256") == key_hash
                ),
                None,
            )
        elif investigation.status in {
            AnalystInvestigationStatus.CANCELLED,
            AnalystInvestigationStatus.FAILED,
        }:
            raise _http_error(409, "investigation_cannot_resume")
        else:
            generation = (
                max(
                    (int(item.input_parameters.get("run_generation", 0)) for item in jobs),
                    default=0,
                )
                + 1
            )
            composition = _composition_factory(request).build(uow, investigation_id)
            if state.stop_reason is not None or investigation.status in {
                AnalystInvestigationStatus.AWAITING_REVIEW,
                AnalystInvestigationStatus.COMPLETED,
                AnalystInvestigationStatus.EXHAUSTED,
            }:
                try:
                    state = await composition.service.resume(
                        investigation_id, owner_job_terminal=True
                    )
                except (VersionConflictError, InvestigationStateVersionConflict) as exc:
                    raise _http_error(409, "stale_investigation_version") from exc
            job, _ = await _submit_run_job(
                request,
                uow,
                investigation_id,
                generation=generation,
                request_key_sha256=key_hash,
                actor_id=identity.actor_id,
            )
        composition = _composition_factory(request).build(uow, investigation_id)
        view = await _investigation_view(
            uow, investigation_id, state=state, composition=composition
        )
        await uow.commit()
    if job is not None and job.status is JobStatus.QUEUED and not replay:
        await _dispatch(request, job)
    return UserRequestResponse(
        investigation=view,
        request_id=request_id,
        job_id=job.id if job else None,
        idempotent_replay=replay,
    )


@router.post(
    "/investigations/{investigation_id}/resume",
    response_model=InvestigationActionResponse,
)
async def resume_investigation(
    investigation_id: UUID,
    payload: InvestigationActionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> InvestigationActionResponse:
    identity = await _identity(request)
    key_hash = _hash_key(idempotency_key)
    async with _uow_factory(request)() as uow:
        _investigation, state = await _require_working_state(uow, investigation_id)
        replay_generation = _find_resume_replay(state, key_hash)
        if replay_generation is not None:
            job = await uow.jobs.get_by_idempotency_key(
                _run_idempotency_key(investigation_id, replay_generation)
            )
            if job is None:
                raise _http_error(409, "idempotency_record_incomplete")
            composition = _composition_factory(request).build(uow, investigation_id)
            view = await _investigation_view(
                uow,
                investigation_id,
                state=state,
                composition=composition,
            )
            return InvestigationActionResponse(
                investigation=view,
                job_id=job.id,
                idempotent_replay=True,
            )
        if state.version != payload.expected_version:
            raise _http_error(409, "stale_investigation_version")
        jobs = await uow.jobs.list_for_aggregate(
            INVESTIGATION_RUN_AGGREGATE_TYPE,
            investigation_id,
            kind=INVESTIGATION_RUN_JOB_KIND,
        )
        if _active_job(jobs) is not None:
            raise _http_error(409, "investigation_job_already_running")
        generation = (
            max(
                (int(job.input_parameters.get("run_generation", 0)) for job in jobs),
                default=0,
            )
            + 1
        )
        composition = _composition_factory(request).build(uow, investigation_id)
        try:
            state = await composition.service.resume(
                investigation_id,
                expected_version=payload.expected_version,
                owner_job_terminal=True,
            )
            marker = f"api:resume:{key_hash}:{generation}"
            state = await _save_idempotency_marker(uow, state, marker)
            job, replay = await _submit_run_job(
                request,
                uow,
                investigation_id,
                generation=generation,
                request_key_sha256=key_hash,
                actor_id=identity.actor_id,
            )
            view = await _investigation_view(
                uow,
                investigation_id,
                state=state,
                composition=composition,
            )
            await uow.commit()
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "stale_investigation_version") from exc
        except ValueError as exc:
            raise _http_error(409, "investigation_resume_conflict", str(exc)) from exc
    await _dispatch(request, job)
    return InvestigationActionResponse(
        investigation=view,
        job_id=job.id,
        idempotent_replay=replay,
    )


@router.post(
    "/investigations/{investigation_id}/interrupt",
    response_model=InvestigationActionResponse,
)
async def interrupt_investigation(
    investigation_id: UUID,
    payload: InvestigationActionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> InvestigationActionResponse:
    identity = await _identity(request)
    key_hash = _hash_key(idempotency_key)
    active_job: Job | None = None
    async with _uow_factory(request)() as uow:
        _investigation, state = await _require_working_state(uow, investigation_id)
        replay_marker = f"api:interrupt:{key_hash}:"
        if any(action.startswith(replay_marker) for action in state.recent_actions):
            jobs = await uow.jobs.list_for_aggregate(
                INVESTIGATION_RUN_AGGREGATE_TYPE,
                investigation_id,
                kind=INVESTIGATION_RUN_JOB_KIND,
            )
            active_job = _active_job(jobs)
            composition = _composition_factory(request).build(uow, investigation_id)
            view = await _investigation_view(
                uow,
                investigation_id,
                state=state,
                composition=composition,
            )
            return InvestigationActionResponse(
                investigation=view,
                job_id=active_job.id if active_job else None,
                idempotent_replay=True,
            )
        if state.version != payload.expected_version:
            raise _http_error(409, "stale_investigation_version")
        jobs = await uow.jobs.list_for_aggregate(
            INVESTIGATION_RUN_AGGREGATE_TYPE,
            investigation_id,
            kind=INVESTIGATION_RUN_JOB_KIND,
        )
        active_job = _active_job(jobs)
        composition = _composition_factory(request).build(uow, investigation_id)
        try:
            state = await composition.service.interrupt(
                investigation_id, expected_version=payload.expected_version
            )
            state = await _save_idempotency_marker(
                uow,
                state,
                f"api:interrupt:{key_hash}:{identity.actor_id[:128]}",
            )
            view = await _investigation_view(
                uow,
                investigation_id,
                state=state,
                composition=composition,
            )
            await uow.commit()
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "stale_investigation_version") from exc
        except ValueError as exc:
            raise _http_error(409, "investigation_interrupt_conflict", str(exc)) from exc
    if active_job is not None:
        await request.app.state.job_service.cancel(active_job.id, actor_id=identity.actor_id)
    return InvestigationActionResponse(
        investigation=view,
        job_id=active_job.id if active_job else None,
        idempotent_replay=False,
    )


@router.get(
    "/subjects/{subject_id}/investigations",
    response_model=list[InvestigationSummaryView],
)
async def list_subject_investigations(
    subject_id: UUID,
    request: Request,
) -> list[InvestigationSummaryView]:
    async with _uow_factory(request)() as uow:
        runs = await uow.production_runs.list_for_subject(subject_id)
        summaries: list[InvestigationSummaryView] = []
        for run in runs:
            investigation = await uow.analyst_investigations.get_for_run(run.id)
            if investigation is None:
                continue
            state = await uow.investigation_states.get(investigation.id)
            if state is None:
                continue
            try:
                snapshot = await _composition_factory(request).article_snapshots.resolve(
                    investigation.synthesis_artifact_id
                )
            except (KeyError, ValueError):
                continue
            if snapshot.subject_id != subject_id or snapshot.production_run_id != run.id:
                continue
            summaries.append(
                InvestigationSummaryView(
                    id=investigation.id,
                    subject_id=investigation.subject_id,
                    production_run_id=investigation.production_run_id,
                    synthesis_artifact_id=investigation.synthesis_artifact_id,
                    synthesis_sha256=snapshot.synthesis_sha256,
                    status=investigation.status,
                    version=state.version,
                    stop_reason=state.stop_reason,
                    updated_at=investigation.updated_at,
                )
            )
        return summaries


def _validate_synthesis_link(
    subject_id: UUID,
    payload: InvestigationStartRequest,
    run: ProductionRun,
    artifact: ProductionArtifact,
) -> None:
    if (
        run.id != payload.production_run_id
        or artifact.production_run_id != run.id
        or artifact.subject_id != subject_id
        or artifact.stage is not ProductionArtifactStage.SYNTHESIS
        or artifact.status is not ProductionArtifactStatus.VERIFIED
    ):
        raise _http_error(409, "synthesis_not_verified")


def _tlp_level(value: str | None) -> TLPLevel | None:
    if value is None:
        return None
    normalized = value.strip().upper().removeprefix("TLP:")
    if normalized == "AMBER+STRICT":
        normalized = "AMBER"
    try:
        return TLPLevel(normalized.lower())
    except ValueError:
        return None


async def _submit_run_job(
    request: Request,
    uow: Any,
    investigation_id: UUID,
    *,
    generation: int,
    request_key_sha256: str,
    actor_id: str,
) -> tuple[Job, bool]:
    parameters = InvestigationRunParameters(
        investigation_id=investigation_id,
        run_generation=generation,
        request_key_sha256=request_key_sha256,
    )
    idempotency_key = _run_idempotency_key(investigation_id, generation)
    job_service = request.app.state.job_service
    try:
        job = await job_service.submit_in_uow(
            uow,
            kind=INVESTIGATION_RUN_JOB_KIND,
            aggregate_type=INVESTIGATION_RUN_AGGREGATE_TYPE,
            aggregate_id=investigation_id,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(),
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=3,
            actor_id=actor_id,
        )
        return job, False
    except DuplicateJobError as exc:
        existing = await uow.jobs.get(exc.existing_job_id)
        if existing is None:
            raise _http_error(409, "investigation_job_idempotency_conflict") from exc
        if (
            existing.aggregate_id != investigation_id
            or existing.kind != INVESTIGATION_RUN_JOB_KIND
            or existing.input_parameters.get("run_generation") != generation
            or existing.input_parameters.get("request_key_sha256") != request_key_sha256
        ):
            raise _http_error(409, "investigation_job_idempotency_conflict") from exc
        return existing, True


def _run_idempotency_key(investigation_id: UUID, generation: int) -> str:
    return f"investigation:{investigation_id}:run:{generation}"


def _user_request_id(action: str) -> UUID | None:
    if not action.startswith(USER_REQUEST_ACTION_PREFIX):
        return None
    try:
        payload = json.loads(action[len(USER_REQUEST_ACTION_PREFIX) :])
        return UUID(str(payload.get("request_id", "")))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _find_resume_replay(state: InvestigationWorkingState, key_hash: str) -> int | None:
    prefix = f"api:resume:{key_hash}:"
    for action in reversed(state.recent_actions):
        if action.startswith(prefix):
            suffix = action[len(prefix) :]
            return int(suffix) if suffix.isdigit() else None
    return None


async def _save_idempotency_marker(
    uow: Any,
    state: InvestigationWorkingState,
    marker: str,
) -> InvestigationWorkingState:
    if marker in state.recent_actions:
        return state
    updated = replace(
        state,
        version=state.version + 1,
        recent_actions=(*state.recent_actions, marker),
    )
    await uow.investigation_states.save_if_version(updated, expected_version=state.version)
    return updated


def _active_job(jobs: list[Job] | tuple[Job, ...]) -> Job | None:
    return next(
        (job for job in jobs if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}),
        None,
    )


async def _require_working_state(
    uow: Any,
    investigation_id: UUID,
) -> tuple[AnalystInvestigation, InvestigationWorkingState]:
    investigation = await uow.analyst_investigations.get(investigation_id)
    if investigation is None:
        raise _http_error(404, "investigation_not_found")
    state = await uow.investigation_states.get(investigation_id)
    if state is None:
        raise _http_error(409, "investigation_not_started")
    return investigation, state


async def _investigation_view(
    uow: Any,
    investigation_id: UUID,
    *,
    composition: InvestigationComposition,
    state: InvestigationWorkingState | None = None,
) -> InvestigationView:
    investigation, current_state = await _require_working_state(uow, investigation_id)
    state = state or current_state
    envelope = await uow.investigation_envelopes.get(investigation_id)
    snapshot = await composition.tool_dependencies.article_snapshots.resolve(
        investigation.synthesis_artifact_id
    )
    all_findings = await uow.investigation_findings.list_for_investigation(investigation_id)
    superseded_ids = {item.supersedes for item in all_findings if item.supersedes is not None}
    findings = tuple(item for item in all_findings if item.id not in superseded_ids)
    findings_by_id = {item.id: item for item in all_findings}
    observations = await uow.evidence_observations.list_for_investigation(investigation_id)
    associations = await uow.sample_associations.list_for_investigation(investigation_id)
    opinion = await uow.analyst_opinions.get(uuid5(_OPINION_NAMESPACE, str(investigation_id)))
    effective_budget = (
        envelope.budget
        if envelope is not None
        else InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: investigation.budget.max_cycles,
                BudgetDimension.VT_UNITS: 0,
                BudgetDimension.SHODAN_CREDITS: 0,
                BudgetDimension.DOWNLOADS: 0,
            }
        )
    )
    budget = {
        dimension.value: BudgetDimensionView(
            limit=effective_budget.limits.get(dimension),
            consumed=effective_budget.consumed[dimension],
            remaining=effective_budget.remaining[dimension],
        )
        for dimension in BudgetDimension
    }
    blocked = _blocked_decisions(state)
    return InvestigationView(
        id=investigation.id,
        subject_id=investigation.subject_id,
        production_run_id=investigation.production_run_id,
        synthesis_artifact_id=investigation.synthesis_artifact_id,
        synthesis_sha256=snapshot.synthesis_sha256,
        status=investigation.status,
        execution_mode=state.execution_mode,
        version=state.version,
        stop_reason=state.stop_reason,
        agenda=[
            AgendaItemView(question=item.question, potential=item.potential, rank=item.rank)
            for item in state.agenda.items
        ],
        hypotheses=[
            HypothesisView(
                id=item.id,
                statement=item.statement,
                status=item.status.value,
                evidence_refs=list(item.evidence_refs),
                finding_refs=list(item.finding_refs),
            )
            for item in state.hypotheses
        ],
        findings=[
            FindingView(
                id=item.id,
                logical_id=_finding_logical_id(item, findings_by_id),
                current_version_id=item.id,
                version=item.version,
                revision_chain=_finding_revision_chain(item, findings_by_id),
                kind=item.kind.value,
                assessment=item.assessment,
                novelty=item.novelty.value if item.novelty else None,
                statement=item.statement,
                scope=item.scope,
                limitations=list(item.limitations),
                observation_refs=list(item.observation_refs),
                claim_refs=list(item.claim_refs),
                created_at=item.created_at,
            )
            for item in findings
        ],
        observations=[
            ObservationView(
                id=item.id,
                kind=item.kind.value,
                sha256=item.sha256,
                source=item.source,
                source_version=item.source_version,
                observed_at=item.observed_at,
                object_type=item.object_type,
                object_id=item.object_id,
                limitations=list(item.limitations),
                invocation_ref=item.invocation_ref,
            )
            for item in observations
        ],
        sample_associations=[
            SampleAssociationView(
                id=item.id,
                sample_sha256=item.sample_sha256,
                origin=item.origin.value,
                qualification=item.qualification,
                related_sample_sha256=list(item.related_sample_sha256),
                version=item.version,
            )
            for item in associations
        ],
        opinion_draft=_opinion_view(opinion),
        budget=budget,
        blocked_decisions=blocked,
        available_tools=list(composition.available_tools),
        unavailable_tools=list(composition.unavailable_tools),
        envelope_approved=envelope is not None,
        allowed_providers=list(envelope.allowed_providers) if envelope is not None else [],
        acquire_hits_allowed=envelope.acquire_hits_allowed if envelope is not None else False,
    )


def _finding_revision_chain(
    current: Any, findings_by_id: Mapping[UUID, Any]
) -> list[FindingRevisionView]:
    chain = []
    seen: set[UUID] = set()
    item = current
    while item is not None and item.id not in seen:
        seen.add(item.id)
        chain.append(
            FindingRevisionView(id=item.id, version=item.version, created_at=item.created_at)
        )
        item = findings_by_id.get(item.supersedes) if item.supersedes is not None else None
    chain.reverse()
    return chain


def _finding_logical_id(current: Any, findings_by_id: Mapping[UUID, Any]) -> UUID:
    chain = _finding_revision_chain(current, findings_by_id)
    return chain[0].id if chain else current.id


def _opinion_view(opinion: AnalystOpinionV1 | None) -> OpinionDraftView | None:
    if opinion is None:
        return None
    return OpinionDraftView(
        id=opinion.id,
        version=opinion.version,
        status=opinion.status.value,
        content_sha256=opinion.content_sha256,
        blocks=[
            OpinionBlockView(
                type=block.block_type.value,
                text=block.text,
                finding_refs=list(block.finding_refs),
                evidence_refs=list(block.evidence_refs),
                qualification=block.qualification.value,
                certainty=block.certainty,
            )
            for block in opinion.blocks
        ],
        unresolved_points=list(opinion.unresolved_points),
        created_at=opinion.created_at,
    )


def _blocked_decisions(state: InvestigationWorkingState) -> list[BlockedDecisionView]:
    blocked_statuses = {
        ToolInvocationStatus.POLICY_DENIED.value,
        ToolInvocationStatus.NEEDS_REVIEW.value,
    }
    result: list[BlockedDecisionView] = []
    for action in state.recent_actions:
        tool_name, separator, rest = action.partition(": ")
        code = rest.partition(":")[0] if separator else ""
        if code in blocked_statuses:
            result.append(BlockedDecisionView(code=code, tool_name=tool_name[:128]))
    if state.stop_reason is StopReason.PERMISSION_REQUIRED:
        result.append(BlockedDecisionView(code="permission_required", tool_name=None))
    return result


async def _current_envelope_view(request: Request, investigation_id: UUID) -> EnvelopeView:
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id)
        snapshot = await composition.tool_dependencies.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        current = await uow.investigation_envelopes.get(investigation_id)
        settings = _composition_factory(request).settings
        if current is None:
            return _envelope_view(
                investigation_id=investigation_id,
                budget=_configured_budget(settings),
                approved=False,
                version=0,
                allowed_providers=(),
                available_providers=_available_providers(composition.available_tools),
                acquire_hits_allowed=False,
                disclosure=_source_disclosure(snapshot),
                approval_decision_id=None,
                approval_actor_id=None,
                approved_at=None,
            )
        return _envelope_view(
            investigation_id=investigation_id,
            budget=current.budget,
            approved=True,
            version=current.version,
            allowed_providers=current.allowed_providers,
            available_providers=_available_providers(composition.available_tools),
            acquire_hits_allowed=current.acquire_hits_allowed,
            disclosure=current.disclosure,
            approval_decision_id=current.approval_decision_id,
            approval_actor_id=current.approval_actor_id,
            approved_at=current.approved_at,
        )


def _configured_budget(settings: Any) -> InvestigationBudget:
    return InvestigationBudget(
        limits={
            BudgetDimension.BRIDGE_TURNS: int(getattr(settings, "investigation_max_cycles", 12)),
            BudgetDimension.VT_UNITS: int(getattr(settings, "investigation_max_vt_read_units", 0)),
            BudgetDimension.SHODAN_CREDITS: int(
                getattr(settings, "investigation_max_shodan_credits", 0)
            ),
            BudgetDimension.DOWNLOADS: int(getattr(settings, "investigation_max_hits_acquired", 0)),
        }
    )


def _budget_from_request(
    payload: EnvelopeDecisionRequest,
    *,
    defaults: InvestigationBudget,
    acquire_default: bool = False,
) -> InvestigationBudget:
    limits = dict(defaults.limits)
    for raw_dimension, amount in payload.budget_limits.items():
        try:
            dimension = BudgetDimension(raw_dimension)
        except ValueError as exc:
            raise _http_error(422, "invalid_budget_dimension", raw_dimension) from exc
        if isinstance(amount, bool) or amount < 0:
            raise _http_error(422, "invalid_budget_limit", raw_dimension)
        limits[dimension] = amount
    if payload.max_acquire_volume is not None:
        limits[BudgetDimension.DOWNLOADS] = payload.max_acquire_volume
    elif (
        payload.acquire_hits_allowed is True
        and BudgetDimension.DOWNLOADS not in payload.budget_limits
    ):
        limits[BudgetDimension.DOWNLOADS] = defaults.limits.get(BudgetDimension.DOWNLOADS, 0)
    if payload.acquire_hits_allowed is False and acquire_default:
        # An explicit false revokes acquisition only through `/revoke` so that the
        # operator records a distinct, auditable G2 revocation action.
        limits[BudgetDimension.DOWNLOADS] = max(
            limits.get(BudgetDimension.DOWNLOADS, 0),
            defaults.limits.get(BudgetDimension.DOWNLOADS, 0),
        )
    return InvestigationBudget(limits=limits)


def _available_providers(available_tools: tuple[str, ...]) -> tuple[str, ...]:
    providers: list[str] = []
    if any(name.startswith("vt.") for name in available_tools):
        providers.append("vt")
    if any(name.startswith("shodan.") for name in available_tools):
        providers.append("shodan")
    if any(name.startswith("advisor.") for name in available_tools):
        # The Bridge advisor is an external LLM provider that G2 must authorize explicitly.
        providers.append("bridge")
    return tuple(providers)


def _requested_providers(
    requested: tuple[str, ...] | list[str], available: tuple[str, ...]
) -> tuple[str, ...]:
    providers = tuple(dict.fromkeys(item.strip().lower() for item in requested))
    if any(item not in {"vt", "shodan", "bridge"} for item in providers):
        raise _http_error(422, "unsupported_provider")
    unavailable = sorted(set(providers).difference(available))
    if unavailable:
        raise _http_error(422, "provider_unavailable", ", ".join(unavailable))
    return providers


def _source_disclosure(snapshot: Any) -> DisclosureBounds:
    return DisclosureBounds(
        tlp=_tlp_level(getattr(snapshot, "tlp", None)),
        do_not_submit=bool(getattr(snapshot, "do_not_submit", True)),
        external_llm_allowed=bool(getattr(snapshot, "external_llm_allowed", False)),
    )


def _disclosure_from_request(
    payload: EnvelopeDisclosureRequest,
    snapshot: Any,
    *,
    current: DisclosureBounds | None = None,
    ceiling: DisclosureBounds | None = None,
) -> DisclosureBounds:
    source = _source_disclosure(snapshot)
    baseline = current or source
    approved_ceiling = ceiling or baseline
    tlp = _tlp_level(payload.tlp) if payload.tlp is not None else baseline.tlp
    if payload.tlp is not None and tlp is None:
        raise _http_error(422, "invalid_tlp")
    do_not_submit = (
        baseline.do_not_submit if payload.do_not_submit is None else payload.do_not_submit
    )
    external_allowed = (
        baseline.external_llm_allowed
        if payload.external_llm_allowed is None
        else payload.external_llm_allowed
    )
    if (source.do_not_submit or approved_ceiling.do_not_submit) and not do_not_submit:
        raise _http_error(422, "disclosure_bounds_cannot_be_relaxed")
    if (
        not source.external_llm_allowed or not approved_ceiling.external_llm_allowed
    ) and external_allowed:
        raise _http_error(422, "disclosure_bounds_cannot_be_relaxed")
    if _tlp_rank(tlp) < max(_tlp_rank(source.tlp), _tlp_rank(approved_ceiling.tlp)):
        raise _http_error(422, "disclosure_bounds_cannot_be_relaxed")
    if do_not_submit or tlp is TLPLevel.RED:
        external_allowed = False
    return DisclosureBounds(
        tlp=tlp,
        do_not_submit=do_not_submit,
        external_llm_allowed=external_allowed,
    )


def _tlp_rank(level: TLPLevel | None) -> int:
    if level is None:
        return 0
    return {
        TLPLevel.WHITE: 1,
        TLPLevel.CLEAR: 1,
        TLPLevel.GREEN: 2,
        TLPLevel.AMBER: 3,
        TLPLevel.RED: 4,
    }[level]


def _decision_correlation(correlation_id: str, idempotency_key: str) -> str:
    key_hash = _hash_key(idempotency_key)[:32]
    return f"{correlation_id.strip()[:80]}:idem:{key_hash}"


def _envelope_view(
    *,
    investigation_id: UUID,
    budget: InvestigationBudget,
    approved: bool,
    version: int,
    allowed_providers: tuple[str, ...],
    available_providers: tuple[str, ...],
    acquire_hits_allowed: bool,
    disclosure: DisclosureBounds,
    approval_decision_id: UUID | None,
    approval_actor_id: str | None,
    approved_at: datetime | None,
) -> EnvelopeView:
    return EnvelopeView(
        investigation_id=investigation_id,
        approved=approved,
        version=version,
        allowed_providers=list(allowed_providers),
        available_providers=list(available_providers),
        acquire_hits_allowed=acquire_hits_allowed,
        max_acquire_volume=budget.limits.get(BudgetDimension.DOWNLOADS, 0),
        budget={
            dimension.value: BudgetDimensionView(
                limit=budget.limits.get(dimension),
                consumed=budget.consumed[dimension],
                remaining=budget.remaining[dimension],
            )
            for dimension in BudgetDimension
        },
        disclosure={
            "tlp": disclosure.tlp.value if disclosure.tlp else None,
            "do_not_submit": disclosure.do_not_submit,
            "external_llm_allowed": disclosure.external_llm_allowed,
        },
        approval_decision_id=approval_decision_id,
        approval_actor_id=approval_actor_id,
        approved_at=approved_at,
    )


def _sample_detail(association: Any) -> SampleDetailView:
    return SampleDetailView(
        id=association.id,
        sample_sha256=association.sample_sha256,
        origin=association.origin,
        qualification=association.qualification,
        related_sample_sha256=list(association.related_sample_sha256),
        version=association.version,
        changes=[
            QualificationChangeView(
                from_qualification=item.from_qualification,
                to_qualification=item.to_qualification,
                actor_kind=item.actor_kind,
                actor_id=item.actor_id or "",
                reason=item.reason,
                changed_at=item.changed_at,
            )
            for item in association.changes
        ],
    )


async def _require_investigation(uow: Any, investigation_id: UUID) -> Any:
    investigation = await uow.analyst_investigations.get(investigation_id)
    if investigation is None:
        raise _http_error(404, "investigation_not_found")
    return investigation


async def _require_evidence_blob(
    uow: Any,
    *,
    investigation_id: UUID,
    blob_id: UUID,
    logical_bucket: str,
    object_type: str,
) -> Any:
    blob = await uow.blobs.get(blob_id)
    if blob is None or blob.descriptor.logical_bucket != logical_bucket:
        raise _http_error(404, "investigation_artifact_not_found")
    observations = await uow.evidence_observations.list_for_investigation(investigation_id)
    if not any(
        item.object_type == object_type
        and item.object_id == blob.descriptor.sha256
        and (object_type == "protocol_model" or item.sha256 == blob.descriptor.sha256)
        for item in observations
    ):
        raise _http_error(404, "investigation_artifact_not_found")
    return blob


async def _read_protocol_model(
    request: Request,
    investigation_id: UUID,
    blob_id: UUID,
) -> ProtocolModelV1:
    async with _uow_factory(request)() as uow:
        investigation = await _require_investigation(uow, investigation_id)
        blob = await _require_evidence_blob(
            uow,
            investigation_id=investigation_id,
            blob_id=blob_id,
            logical_bucket=_PROTOCOL_MODEL_BUCKET,
            object_type="protocol_model",
        )
    model = ProtocolModelV1.from_dict(await _artifact_store(request).read_json(blob.id))
    if model.investigation_id != investigation_id or model.subject_id != str(
        investigation.subject_id
    ):
        raise _http_error(404, "protocol_model_not_found")
    return model


def _artifact_store(request: Request) -> ProductionArtifactStore:
    configured = getattr(request.app.state, "production_artifact_store", None)
    if configured is None:
        raise _http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "investigation_artifacts_unavailable",
        )
    return cast(ProductionArtifactStore, configured)


async def _pivot_observation_payloads(
    request: Request, uow: Any, observations: Sequence[Any]
) -> dict[str, Mapping[str, Any] | None]:
    payloads: dict[str, Mapping[str, Any] | None] = {}
    for observation in observations:
        source = observation.source.casefold()
        if (
            observation.kind.value != "vt_relationship"
            and observation.invocation_ref is None
            and source != "sample_similarity"
            and not source.startswith("network.capture.")
        ):
            continue
        blob = await uow.blobs.get(observation.blob_id)
        descriptor = getattr(blob, "descriptor", None)
        if descriptor is None:
            continue
        mime_type = descriptor.mime_type.split(";", 1)[0].strip().casefold()
        if mime_type != "application/json" or getattr(descriptor, "size", 0) > 128 * 1024:
            continue
        try:
            raw = await _artifact_store(request).read_bytes(
                observation.blob_id, max_bytes=128 * 1024
            )
        except (KeyError, ValueError):
            continue
        if hashlib.sha256(raw).hexdigest() != observation.sha256:
            continue
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            payloads[str(observation.id)] = value
    return payloads


def _comparison_summary(raw: dict[str, Any], blob_id: UUID, sha256: str) -> dict[str, Any]:
    features = raw.get("features")
    limits = raw.get("limits")
    return {
        "blob_id": blob_id,
        "sha256": sha256,
        "schema_version": raw.get("schema_version"),
        "status": raw.get("status"),
        "sample_sha256s": list(raw.get("comparison_sample_sha256s", []))[:32],
        "sample_count": len(raw.get("comparison_sample_sha256s", [])),
        "feature_count": len(features) if isinstance(features, list) else 0,
        "limits": _bounded_comparison_limits(limits),
        "warnings": list(raw.get("warnings", []))[:16],
    }


def _bounded_comparison(
    raw: dict[str, Any], blob_id: UUID, sha256: str, *, feature_limit: int
) -> dict[str, Any]:
    features = raw.get("features")
    if not isinstance(features, list):
        features = []
    return {
        "blob_id": blob_id,
        "sha256": sha256,
        "schema_version": raw.get("schema_version"),
        "investigation_id": raw.get("investigation_id"),
        "corpus_snapshot_sha256": raw.get("corpus_snapshot_sha256"),
        "status": raw.get("status"),
        "sample_sha256s": list(raw.get("comparison_sample_sha256s", []))[:32],
        "sample_count": len(raw.get("comparison_sample_sha256s", [])),
        "features": [_bounded_comparison_feature(item) for item in features[:feature_limit]],
        "feature_count": len(features),
        "features_truncated": len(features) > feature_limit,
        "limits": _bounded_comparison_limits(raw.get("limits")),
        "provenance": {
            "extractor_versions": list(raw.get("extractor_versions", []))[:64],
            "policy": raw.get("policy", {}),
        },
        "warnings": list(raw.get("warnings", []))[:32],
        "recommended_next_tests": list(raw.get("recommended_next_tests", []))[:32],
    }


def _bounded_comparison_limits(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        **raw,
        "packs_missing": list(raw.get("packs_missing", []))[:32],
        "per_sample_feature_limit_applied": list(raw.get("per_sample_feature_limit_applied", []))[
            :32
        ],
        "feature_losses": list(raw.get("feature_losses", []))[:32],
    }


def _bounded_comparison_feature(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    bounded = dict(raw)
    value = bounded.get("value")
    if isinstance(value, str) and len(value) > 512:
        bounded["value"] = value[:512]
        bounded["value_truncated"] = True
    bounded["rationale"] = list(raw.get("rationale", []))[:16]
    bounded["rejection_reasons"] = list(raw.get("rejection_reasons", []))[:16]
    presences = raw.get("sample_presence", [])
    bounded["sample_presence"] = (
        [
            {
                **item,
                "offsets": list(item.get("offsets", []))[:32],
                "function_offsets": list(item.get("function_offsets", []))[:32],
                "sources": list(item.get("sources", []))[:16],
            }
            for item in presences[:64]
            if isinstance(item, dict)
        ]
        if isinstance(presences, list)
        else []
    )
    candidates = raw.get("signature_candidates", [])
    bounded["signature_candidates"] = (
        [
            {
                **item,
                "pattern": str(item.get("pattern", ""))[:512],
                "positive_sample_sha256s": list(item.get("positive_sample_sha256s", []))[:32],
                "source_offsets": list(item.get("source_offsets", []))[:32],
                "rejection_reasons": list(item.get("rejection_reasons", []))[:16],
            }
            for item in candidates[:16]
            if isinstance(item, dict)
        ]
        if isinstance(candidates, list)
        else []
    )
    return bounded


def _protocol_model_summary(model: ProtocolModelV1, blob_id: UUID, sha256: str) -> dict[str, Any]:
    return {
        "blob_id": blob_id,
        "sha256": sha256,
        "schema_version": model.schema_version,
        "model_version": model.model_version,
        "protocol": model.protocol,
        "transport": model.transport.value,
        "validation_status": model.validation_status.value,
        "family_assurance": model.family_assurance.value,
        "sample_sha256s": list(model.sample_sha256s[:32]),
        "message_count": len(model.messages),
        "field_count": len(model.fields),
        "transition_count": len(model.transitions),
    }


def _bounded_protocol_model(model: ProtocolModelV1, blob_id: UUID) -> dict[str, Any]:
    payload = model.to_dict()
    bounds = {
        "endpoints": 64,
        "conversations": 64,
        "messages": 128,
        "fields": 256,
        "transitions": 128,
        "function_provenance": 64,
        "verification_findings": 128,
        "test_plan": 64,
        "limitations": 64,
        "extractor_versions": 64,
        "sample_sha256s": 32,
        "capture_sha256s": 32,
        "report_ids": 32,
    }
    truncated: dict[str, bool] = {}
    for key, maximum in bounds.items():
        value = payload.get(key)
        if isinstance(value, list):
            truncated[key] = len(value) > maximum
            payload[key] = value[:maximum]
    return {
        "blob_id": blob_id,
        "sha256": model.canonical_sha256,
        "model": payload,
        "truncated": truncated,
    }


def _finding_evidence_view(finding: Any) -> dict[str, Any]:
    return {
        "id": finding.id,
        "kind": finding.kind.value,
        "assessment": finding.assessment.value,
        "statement": finding.statement,
        "observation_refs": list(finding.observation_refs[:64]),
        "limitations": list(finding.limitations[:32]),
        "contradictory": finding.assessment is FindingAssessment.REFUTED,
    }


def _observation_evidence_view(observation: Any) -> dict[str, Any]:
    return {
        "id": observation.id,
        "kind": observation.kind.value,
        "sha256": observation.sha256,
        "source": observation.source,
        "source_version": observation.source_version,
        "observed_at": observation.observed_at,
        "limitations": list(observation.limitations[:32]),
    }


def _data_url(mime_type: str, content: bytes) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


async def _pending_prompt_tool_inventory(
    state: InvestigationWorkingState,
    *,
    artifact_store: Any,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Use the exact serialized inventory while that prompt is awaiting Bridge."""
    snapshot_id = state.pending_context_snapshot_id
    reader = getattr(artifact_store, "read_json", None)
    if snapshot_id is None or not callable(reader):
        return None
    try:
        stored = await reader(snapshot_id)
    except Exception:
        return None
    context = stored.get("context") if isinstance(stored, dict) else None
    if (
        not isinstance(context, dict)
        or context.get("schema") != "investigation-context.v1"
        or context.get("investigation_id") != str(state.investigation_id)
    ):
        return None
    raw_available = context.get("available_tools")
    raw_unavailable = context.get("unavailable_tools", [])
    if not isinstance(raw_available, list) or not isinstance(raw_unavailable, list):
        return None
    available_names: set[str] = set()
    for item in raw_available:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) is None:
            return None
        available_names.add(name)
    if any(
        not isinstance(item, str)
        or len(item) > 256
        or re.fullmatch(r"[A-Za-z0-9._-]{1,128}:[A-Za-z0-9_.:-]{1,96}", item) is None
        for item in raw_unavailable[:64]
    ):
        return None
    return tuple(sorted(available_names)), tuple(raw_unavailable[:64])


async def _operator_status_tool_inventory(
    *,
    state: InvestigationWorkingState,
    composition: InvestigationComposition,
    envelope: Any | None,
    invocations: tuple[Any, ...],
    observations: tuple[Any, ...],
    external_llm_allowed: bool | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Use the registry projection shared with prompt construction and dispatch."""
    registry = getattr(composition, "registry", None)
    available_capabilities = getattr(composition, "available_capabilities", None)
    if registry is None or available_capabilities is None:
        return None
    context = ToolExecutionContext(
        investigation_id=state.investigation_id,
        envelope=envelope,
        budget=envelope.budget if envelope is not None else InvestigationBudget(limits={}),
        idempotency_key="operator-status-tool-availability",
        available_capabilities=available_capabilities,
    )
    blocked_capabilities = await blocked_capabilities_from_history(
        investigation_id=state.investigation_id,
        invocations=invocations,
        observations=observations,
    )
    if external_llm_allowed is False:
        blocked_capabilities = (
            *blocked_capabilities,
            "bridge.advisor:external_llm_disclosure_denied",
        )
    context = replace(
        context,
        blocked_capabilities=blocked_capabilities,
    )
    availability = project_tool_availability(registry, context)
    dependencies = getattr(composition, "tool_dependencies", None)
    pending = await _pending_prompt_tool_inventory(
        state,
        artifact_store=getattr(dependencies, "artifact_store", None),
    )
    if pending is not None:
        return pending
    return availability.available_names, availability.unavailable_diagnostics[:64]


def _unavailable_tool_diagnostics(
    request: Request, composition: InvestigationComposition
) -> tuple[str, ...]:
    """Compatibility projection for route fakes predating the live registry contract."""
    runtime = getattr(request.app.state, "investigation_runtime_composition", None)
    ports = getattr(runtime, "ports", None)
    statuses = getattr(ports, "statuses", ())
    reasons = {
        str(item.family): item.reason
        for item in statuses
        if getattr(item, "family", None) is not None
    }
    aliases = {
        "acquisition": "virustotal",
        "local_commands": "local_command_execution",
        "rule_workbench": "rule_workbench",
    }
    prefixes = (
        ("vt.retrohunt.", "retrohunt"),
        ("vt.", "virustotal"),
        ("shodan.", "shodan"),
        ("local.", "local_command_execution"),
        ("rule.", "rule_workbench"),
        ("ghidra.", "ghidra"),
        ("network.", "network_capture"),
        ("features.", "sample_similarity"),
        ("samples.", "sample_similarity"),
    )
    exact = {
        "sample.analyze": "local_analysis",
        "sample.extract_obfuscated_strings": "floss_extraction",
        "sample.extract_configuration": "configuration_extraction",
        "sandbox.report.import": "behavior_report_import",
        "signatures.candidates": "sample_similarity",
        "native.inspect": "native_analysis",
    }

    def family_for_tool(name: str) -> str:
        if name in aliases:
            return aliases[name]
        if name in exact:
            return exact[name]
        return next((family for prefix, family in prefixes if name.startswith(prefix)), name)

    def reason_code(value: object) -> str:
        if (
            isinstance(value, str)
            and 0 < len(value) <= 96
            and value[0].islower()
            and value.isascii()
            and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_:.-" for character in value)
        ):
            return value
        return "dependency_status_unavailable"

    return tuple(
        f"{name}:{reason_code(reasons.get(family_for_tool(name)))}"
        for name in composition.unavailable_tools
    )


def _composition_factory(request: Request) -> InvestigationCompositionFactory:
    factory = getattr(request.app.state, "investigation_composition_factory", None)
    if factory is None:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "investigation_unavailable")
    return cast(InvestigationCompositionFactory, factory)


def _uow_factory(request: Request) -> Any:
    factory = getattr(request.app.state, "uow_factory", None)
    if factory is None:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "investigation_unavailable")
    return factory


async def _identity(request: Request) -> Any:
    current = getattr(request.state, "identity", None)
    if current is not None:
        return current
    provider: IdentityProvider = request.app.state.identity_provider
    return await provider.current()


async def _require_operator_client(request: Request) -> None:
    identity = await _identity(request)
    if not identity.has_scope(IdentityScope.OPERATOR_CLIENT):
        raise _http_error(403, "operator_client_scope_required")


async def _require_article_claim_reader(request: Request, section: str) -> None:
    identity = await _identity(request)
    if identity.has_scope(IdentityScope.OPERATOR_CLIENT):
        return
    if section == "article_claims" and identity.has_scope(IdentityScope.HUMAN_OPERATOR):
        provider = getattr(request.app.state, "identity_provider", None)
        provider_mode = getattr(provider, "mode", None)
        if provider_mode == "local" or (
            provider_mode == "authenticated" and getattr(provider, "verified", False) is True
        ):
            return
    raise _http_error(403, "operator_client_scope_required")


async def _dispatch(request: Request, job: Job) -> None:
    dispatcher = getattr(request.app.state, "job_dispatcher", None)
    if dispatcher is None:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "job_dispatch_unavailable")
    await dispatcher.dispatch(job.id)


def _bounded_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    errors = exc.errors(include_input=False, include_context=False)[:8]
    return [
        {
            "loc": ".".join(str(item) for item in error.get("loc", ())[:6])[:160],
            "type": str(error.get("type", "validation_error"))[:64],
        }
        for error in errors
    ]


def _http_error(
    code_status: int,
    code: str,
    message: str | None = None,
    *,
    details: Mapping[str, Any] | None = None,
) -> HTTPException:
    detail: dict[str, Any] = {"code": code}
    if message:
        detail["message"] = message
    if details:
        detail["details"] = dict(details)
    return HTTPException(status_code=code_status, detail=detail)


class InvocationReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    outcome: str = Field(
        pattern=r"^(executed_with_result|not_executed|abandon)$",
    )
    reason: str = Field(min_length=1, max_length=4_000)
    response_refs: tuple[UUID, ...] = Field(default=(), max_length=128)


class InvocationReconciliationResponse(BaseModel):
    invocation_id: UUID
    status: ToolInvocationStatus
    version: int
    response_refs: tuple[UUID, ...]


@router.post(
    "/investigations/{investigation_id}/invocations/{invocation_id}/reconcile",
    response_model=InvocationReconciliationResponse,
)
async def reconcile_investigation_invocation(
    investigation_id: UUID,
    invocation_id: UUID,
    payload: InvocationReconciliationRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> InvocationReconciliationResponse:
    from cti_app.application.investigation_service import InvocationReconciliationOutcome

    identity = await _identity(request)
    async with _uow_factory(request)() as uow:
        try:
            composition = _composition_factory(request).build(uow, investigation_id)
            invocation = await composition.service.reconcile_invocation(
                investigation_id,
                invocation_id,
                outcome=InvocationReconciliationOutcome(payload.outcome),
                actor=identity.actor_id,
                reason=payload.reason,
                response_refs=payload.response_refs,
                expected_version=payload.expected_version,
                idempotency_key=_decision_correlation(
                    f"invocation:{invocation_id}", idempotency_key
                ),
            )
            await uow.commit()
        except LookupError as exc:
            raise _http_error(status.HTTP_404_NOT_FOUND, "invocation_not_found") from exc
        except VersionConflictError as exc:
            raise _http_error(status.HTTP_409_CONFLICT, "stale_invocation_version") from exc
        except ValueError as exc:
            raise _http_error(
                status.HTTP_409_CONFLICT, "invocation_reconciliation_conflict"
            ) from exc
        except RuntimeError as exc:
            raise _http_error(
                status.HTTP_503_SERVICE_UNAVAILABLE, "invocation_audit_unavailable"
            ) from exc
    return InvocationReconciliationResponse(
        invocation_id=invocation.id,
        status=invocation.status,
        version=invocation.version,
        response_refs=invocation.response_refs,
    )

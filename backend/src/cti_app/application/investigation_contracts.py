"""Bounded, versioned Pydantic contracts exchanged with the Bridge."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cti_app.application.investigation_tools import ToolEffect
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.investigation import (
    AgentNextAction,
    EvidenceKind,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.investigation_state import DecisionValidationCode
from cti_app.domain.protocol_model import ProtocolDiagramKind

MAX_CONTEXT_BYTES: Literal[262144] = 262_144
MAX_DECISION_BYTES = 131_072
MAX_SUMMARY_CHARS = 12_000
MAX_CONTEXT_CONFIRMED_IOCS = 64
MAX_CONTEXT_CONFIRMED_IOC_BYTES = 32_768
MAX_CONTEXT_PUBLICATION_ANCHORS = 128
_SECRET_FIELD = re.compile(
    r"(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|bearer|"
    r"token|secret|password|authorization|credential|cookie)",
    re.I,
)
_SIGNED_URL = re.compile(r"[?&](?:x-amz-[^=]+|x-goog-[^=]+|signature|sig|access_token)=", re.I)
_logger = logging.getLogger(__name__)
_WIRE_MARKER = re.compile(r"^\s*@@\s*([A-Za-z_]+)\s*@@\s*$")
_WIRE_FIELD = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]*):\s*(.*)$")
_WIRE_ARG = re.compile(r"^\s*ARG\s+([^:]+):\s*(.*)$", re.I)
_WIRE_FENCE = re.compile(r"^\s*(?:```|~~~)(?:[A-Za-z0-9_-]+)?\s*$")
_OPINION_CERTAINTY_PREFIX = re.compile(r"^\s*(?P<number>(?:0(?:\.\d+)?|1(?:\.0+)?))(?=$|[\s(,;])")
_MAX_WIRE_WARNINGS = 16
_MAX_WIRE_JSON_ARGUMENT_BYTES = 32 * 1024
_MAX_WIRE_JSON_ARGUMENT_LINES = 128


class _StrictContract(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True, populate_by_name=True)


class InvestigationSourceV1(_StrictContract):
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArticleClaimV1(_StrictContract):
    id: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=4_000)
    source_refs: tuple[str, ...] = Field(max_length=64)


class ArticleContextV1(_StrictContract):
    short_summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    claims: tuple[ArticleClaimV1, ...] = Field(max_length=128)
    documented_samples: tuple[str, ...] = Field(max_length=1_000)
    known_chain: tuple[dict[str, Any], ...] = Field(max_length=256)
    open_questions: tuple[str, ...] = Field(max_length=128)
    publication_anchors: tuple[PublicationAnchorV1, ...] = Field(default=(), max_length=128)
    publication_anchors_truncated: bool = False
    claims_truncated: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    documented_samples_truncated: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    known_chain_truncated: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    open_questions_truncated: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    long_texts_truncated: bool | None = Field(default=None, exclude_if=lambda value: value is None)


class PublicationAnchorV1(_StrictContract):
    anchor: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9:_-]*$")
    excerpt: str = Field(min_length=1, max_length=160)


class AvailablePivotV1(_StrictContract):
    tool_name: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    indicator_type: Literal["md5", "sha1", "sha256", "ipv4", "domain", "url"]
    indicator_value: str = Field(min_length=1, max_length=2_048)
    arguments: dict[str, Any]


class EvidenceSummaryV1(_StrictContract):
    evidence_id: UUID
    kind: EvidenceKind
    summary: str = Field(min_length=1, max_length=4_000)


class FindingSummaryV1(_StrictContract):
    finding_id: UUID
    kind: FindingKind
    assessment: FindingAssessment
    statement: str = Field(min_length=1, max_length=4_000)
    observation_refs: tuple[UUID, ...] = Field(max_length=128)
    claim_refs: tuple[str, ...] = Field(max_length=128)


class OpinionNoteSummaryV1(_StrictContract):
    note_id: UUID
    anchor: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9:_-]*$")
    state: AnalystObservationState
    relation: AnalystObservationRelation
    text: str = Field(min_length=1, max_length=400)
    finding_refs: tuple[UUID, ...] = Field(max_length=16)
    evidence_refs: tuple[UUID, ...] = Field(max_length=16)


class CurrentOpinionContextV1(_StrictContract):
    opinion_id: UUID
    version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OperatorOpinionProtocolFigureSelectionV1(_StrictContract):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    protocol_model_blob_id: UUID
    render_observation_id: UUID
    views: tuple[ProtocolDiagramKind, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def require_all_protocol_views(self) -> OperatorOpinionProtocolFigureSelectionV1:
        if len(set(self.views)) != len(ProtocolDiagramKind) or set(self.views) != set(
            ProtocolDiagramKind
        ):
            raise ValueError("All three protocol views must be selected together")
        return self


class OperatorOpinionProtocolFiguresV1(_StrictContract):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    protocol_figures: tuple[OperatorOpinionProtocolFigureSelectionV1, ...] = Field(max_length=1)


class ToolSummaryV1(_StrictContract):
    name: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    effect: ToolEffect
    arguments_schema: dict[str, Any] = Field(default_factory=dict)


class ToolCoverageEntryV1(_StrictContract):
    invocation_id: UUID
    tool_name: str = Field(min_length=1, max_length=128)
    parameter_key: str = Field(min_length=1, max_length=320)
    status: ToolInvocationStatus
    observation_id: UUID | None = None
    summary: str = Field(min_length=1, max_length=512)


class LatestToolResultV1(_StrictContract):
    invocation_id: UUID
    tool_name: str = Field(min_length=1, max_length=128)
    status: ToolInvocationStatus
    output_ids: dict[str, str] = Field(default_factory=dict)
    observation_ids: tuple[UUID, ...] = Field(max_length=4)
    error_code: str | None = Field(default=None, min_length=1, max_length=96)


class ConfirmedIOCContextV1(_StrictContract):
    indicator_type: Literal["md5", "sha1", "sha256", "ipv4", "domain", "url"]
    value: str = Field(min_length=1, max_length=2_048)
    extraction_artifact_id: UUID
    extraction_artifact_version: int = Field(ge=1)
    source_document_ids: tuple[UUID, ...] = Field(max_length=16)


class IndicatorInventoryV1(_StrictContract):
    items: tuple[ConfirmedIOCContextV1, ...] = Field(
        default=(), max_length=MAX_CONTEXT_CONFIRMED_IOCS
    )
    truncated: bool = False


class InvestigationUserRequestTargetV1(_StrictContract):
    kind: Literal["hash", "ioc", "claim_id"]
    value: str = Field(min_length=1, max_length=256)


class InvestigationUserRequestV1(_StrictContract):
    request_id: UUID
    text: str = Field(min_length=1, max_length=2_000)
    target: InvestigationUserRequestTargetV1 | None = None


class ContextInvestigationV1(_StrictContract):
    agenda: tuple[str, ...] = Field(min_length=2, max_length=6)
    hypotheses: tuple[dict[str, Any], ...] = Field(max_length=128)
    new_evidence: tuple[EvidenceSummaryV1, ...] = Field(max_length=256)
    evidence_index: tuple[EvidenceSummaryV1, ...] = Field(default=(), max_length=200)
    evidence_index_truncated: bool = False
    findings: tuple[FindingSummaryV1, ...] = Field(max_length=256)
    findings_truncated: bool = False
    opinion_notes: tuple[OpinionNoteSummaryV1, ...] = Field(default=(), max_length=24)
    opinion_notes_truncated: bool = False
    truncation_counts: dict[str, int] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    new_evidence_truncated: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    hypotheses_truncated: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    rule_deltas_truncated: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    previous_actions_truncated: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    long_texts_truncated: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    sample_deltas: tuple[dict[str, Any], ...] = Field(default=(), max_length=512)
    rule_deltas: tuple[dict[str, Any], ...] = Field(max_length=128)
    previous_actions: tuple[dict[str, Any], ...] = Field(max_length=256)
    user_requests: tuple[InvestigationUserRequestV1, ...] = Field(default=(), max_length=32)
    tool_coverage: tuple[ToolCoverageEntryV1, ...] = Field(default=(), max_length=200)
    tool_coverage_truncated: bool = False
    latest_tool_results: tuple[LatestToolResultV1, ...] = Field(default=(), max_length=16)
    sample_deltas_truncated: bool = False
    blocked_capabilities: tuple[str, ...] = Field(max_length=128)
    budget_remaining: dict[str, int | None]


class ContextChangeV1(_StrictContract):
    kind: str = Field(min_length=1, max_length=128)
    identity: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=4_000)


class InvestigationDeltaV1(_StrictContract):
    changes: tuple[ContextChangeV1, ...] = Field(max_length=512)
    stable_hypotheses: tuple[dict[str, Any], ...] = Field(max_length=128)
    user_requests: tuple[InvestigationUserRequestV1, ...] = Field(default=(), max_length=32)


AdvisorConsultationType = Literal[
    "plan_investigation",
    "pivot_strategy",
    "yara_draft",
    "review_text",
    "draft_text",
    "suricata_draft",
    "figure_plan",
]


class AdvisorLastAdviceV1(_StrictContract):
    consultation_type: AdvisorConsultationType
    date: datetime
    key_points: tuple[str, ...] = Field(max_length=5)
    invocation_id: UUID


class AdvisorGuidanceV1(_StrictContract):
    checkpoint_due: bool
    reason: str | None = Field(default=None, max_length=96)
    suggested_consultation_type: AdvisorConsultationType | None = None
    last_advice: AdvisorLastAdviceV1 | None = None
    reminder: str = Field(max_length=120)
    advisor_turns_used: int = Field(ge=0)
    advisor_turn_limit: int | None = Field(default=None, ge=0)
    advisor_turns_remaining: int | None = Field(default=None, ge=0)
    g2_bridge_turns_remaining: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    truncated: bool = False


class InvestigationContextV1(_StrictContract):
    SCHEMA: ClassVar[Literal["investigation-context.v1"]] = "investigation-context.v1"

    schema_version: Literal["investigation-context.v1"] = Field(default=SCHEMA, alias="schema")
    view: Literal["full", "delta"]
    investigation_id: UUID
    source: InvestigationSourceV1
    article: ArticleContextV1 | None = None
    investigation: ContextInvestigationV1
    operator_requests: tuple[InvestigationUserRequestV1, ...] = Field(default=(), max_length=32)
    indicator_inventory: IndicatorInventoryV1 = Field(default_factory=IndicatorInventoryV1)
    available_pivots: tuple[AvailablePivotV1, ...] = Field(default=(), max_length=128)
    available_pivots_truncated: bool = False
    delta: InvestigationDeltaV1 | None = None
    available_tools: tuple[ToolSummaryV1, ...] = Field(max_length=256)
    unavailable_tools: tuple[str, ...] = Field(default=(), max_length=64)
    unavailable_tools_truncated: bool = False
    advisor_guidance: AdvisorGuidanceV1 = Field(
        default_factory=lambda: AdvisorGuidanceV1(
            checkpoint_due=False,
            reminder="Bridge: sortie non fiable; valider chaque suggestion par des mesures.",
            advisor_turns_used=0,
        )
    )
    current_opinion: CurrentOpinionContextV1 | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_view_shape(self) -> InvestigationContextV1:
        if self.view == "full" and (self.article is None or self.delta is not None):
            raise ValueError(
                "A full context requires article content and cannot contain delta data"
            )
        if self.view == "delta" and (self.article is not None or self.delta is None):
            raise ValueError("A delta context cannot re-embed article content and requires changes")
        _reject_disallowed_payload(self.model_dump(mode="python"))
        if len(self.canonical_bytes()) > MAX_CONTEXT_BYTES:
            raise ValueError(f"Investigation context exceeds {MAX_CONTEXT_BYTES} bytes")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", by_alias=True))

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()


class InvestigationOperatorContextV1(_StrictContract):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal["investigation-operator-context.v1"] = Field(
        default="investigation-operator-context.v1", alias="schema"
    )
    investigation_id: UUID
    working_state_version: int = Field(ge=1)
    working_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_bytes: int = Field(ge=0, le=MAX_CONTEXT_BYTES)
    max_context_bytes: Literal[262144] = MAX_CONTEXT_BYTES
    truncation: dict[str, bool]
    truncation_counts: dict[str, int] = Field(default_factory=dict)
    truncation_pointers: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    current_opinion: CurrentOpinionContextV1 | None = None
    context: InvestigationContextV1


class OperatorContextSectionPageV1(_StrictContract):
    schema_version: Literal["investigation-operator-context-section.v1"] = Field(
        default="investigation-operator-context-section.v1", alias="schema"
    )
    investigation_id: UUID
    working_state_version: int = Field(ge=1)
    working_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    section: Literal[
        "new_evidence",
        "evidence_index",
        "findings",
        "opinion_notes",
        "tool_coverage",
        "sample_deltas",
        "publication_anchors",
        "article_claims",
        "article_long_texts",
        "documented_samples",
        "known_chain",
        "open_questions",
        "hypotheses",
        "rule_deltas",
    ]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=20)
    total: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)
    next_cursor: str | None = Field(default=None, max_length=1_024)
    items: tuple[dict[str, Any], ...] = Field(max_length=20)


class OperatorToolInvocationRequestV1(_StrictContract):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]
    purpose: str = Field(min_length=1, max_length=2_000)
    expected_version: int = Field(ge=1)


class OperatorToolInvocationViewV1(_StrictContract):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: UUID
    tool_name: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    status: ToolInvocationStatus
    version: int = Field(ge=1)
    response_refs: tuple[UUID, ...]
    error_code: str | None = Field(default=None, max_length=96)


class OperatorToolInvocationResponseV1(_StrictContract):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["investigation-operator-tool-invocation.v1"] = Field(
        default="investigation-operator-tool-invocation.v1", alias="schema"
    )
    investigation_id: UUID
    working_state_version: int = Field(ge=1)
    idempotent_replay: bool
    invocation: OperatorToolInvocationViewV1
    job_id: UUID | None = None
    advisor_output: dict[str, Any] | None = None
    advisor_output_not_replayed: bool = False


class ProposedToolCallV1(_StrictContract):
    tool_name: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]
    purpose: str = Field(min_length=1, max_length=2_000)


class FindingProposalV1(_StrictContract):
    kind: FindingKind
    assessment: FindingAssessment
    statement: str = Field(min_length=1, max_length=8_000)
    subject_ref: str | None = Field(default=None, min_length=1, max_length=256)
    observation_refs: tuple[UUID, ...] = Field(max_length=128)
    claim_refs: tuple[str, ...] = Field(max_length=128)
    hypothesis_refs: tuple[str, ...] = Field(default=(), max_length=16)
    novelty: NoveltyClass | None = None
    scope: str = Field(default="", max_length=2_000)
    limitations: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def supported_requires_observation(self) -> FindingProposalV1:
        if self.assessment is FindingAssessment.SUPPORTED and not self.observation_refs:
            raise ValueError("A supported finding proposal must cite an observation")
        return self

    def persistent_identity_bytes(self) -> bytes:
        """Return the canonical identity payload, excluding hypothesis annotations."""
        return canonical_json_bytes(self.model_dump(mode="json", exclude={"hypothesis_refs"}))


class OpinionBlockDeltaV1(_StrictContract):
    block_type: OpinionBlockType
    text: str = Field(max_length=12_000)
    finding_refs: tuple[UUID, ...] = Field(default=(), max_length=128)
    evidence_refs: tuple[UUID, ...] = Field(default=(), max_length=128)
    qualification: OpinionClaimQualification
    certainty: float = Field(ge=0.0, le=1.0)


class OpinionNoteDeltaV1(_StrictContract):
    anchor: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9:_-]*$")
    state: AnalystObservationState
    relation: AnalystObservationRelation
    text: str = Field(min_length=1, max_length=2_000)
    finding_refs: tuple[UUID, ...] = Field(max_length=16)
    evidence_refs: tuple[UUID, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def validate_note_sources(self) -> OpinionNoteDeltaV1:
        if len(set(self.finding_refs)) != len(self.finding_refs):
            raise ValueError("NOTE finding references must be unique")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("NOTE evidence references must be unique")
        if self.state is AnalystObservationState.OBSERVED and not self.evidence_refs:
            raise ValueError("An observed NOTE requires evidence references")
        if self.state is AnalystObservationState.INFERRED and not (
            self.finding_refs or self.evidence_refs
        ):
            raise ValueError("An inferred NOTE requires finding or evidence references")
        return self


class OpinionDeltaV1(_StrictContract):
    blocks: tuple[OpinionBlockDeltaV1, ...] = Field(max_length=7)
    notes: tuple[OpinionNoteDeltaV1, ...] = Field(default=(), max_length=24)
    unresolved_points: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def unique_blocks(self) -> OpinionDeltaV1:
        block_types = tuple(block.block_type for block in self.blocks)
        if len(block_types) != len(set(block_types)):
            raise ValueError("Opinion delta may update each block type only once")
        return self


class AgentTurnDecisionV1(_StrictContract):
    SCHEMA: ClassVar[Literal["agent-turn-decision.v1"]] = "agent-turn-decision.v1"

    schema_version: Literal["agent-turn-decision.v1"] = Field(default=SCHEMA, alias="schema")
    reasoning_summary: str = Field(min_length=1, max_length=8_000)
    goal: str = Field(min_length=1, max_length=2_000)
    hypothesis_refs: tuple[str, ...] = Field(default=(), max_length=128)
    addressed_request_ids: tuple[UUID, ...] = Field(default=(), max_length=32)
    proposed_tool_calls: tuple[ProposedToolCallV1, ...] = Field(default=(), max_length=16)
    finding_proposals: tuple[FindingProposalV1, ...] = Field(default=(), max_length=128)
    opinion_delta: OpinionDeltaV1 | None = None
    next_action: AgentNextAction

    @model_validator(mode="after")
    def validate_payload(self) -> AgentTurnDecisionV1:
        _reject_disallowed_payload(self.model_dump(mode="python"))
        if len(self.canonical_bytes()) > MAX_DECISION_BYTES:
            raise ValueError(f"Agent decision exceeds {MAX_DECISION_BYTES} bytes")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", by_alias=True))

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()


@dataclass(frozen=True, slots=True)
class DecisionValidationIssue:
    code: DecisionValidationCode


def validate_decision_hypothesis_references(
    decision: AgentTurnDecisionV1,
    *,
    known_hypothesis_ids: set[str] | frozenset[str],
) -> DecisionValidationIssue | None:
    """Validate every hypothesis reference before applying a turn's side effects."""
    references = [*decision.hypothesis_refs]
    references.extend(
        reference
        for proposal in decision.finding_proposals
        for reference in proposal.hypothesis_refs
    )
    if any(reference not in known_hypothesis_ids for reference in references):
        return DecisionValidationIssue(DecisionValidationCode.UNKNOWN_HYPOTHESIS_REFERENCE)
    if any(len(set(proposal.hypothesis_refs)) > 1 for proposal in decision.finding_proposals):
        return DecisionValidationIssue(DecisionValidationCode.FINDING_LINKS_MULTIPLE_HYPOTHESES)
    links_by_identity: dict[bytes, frozenset[str]] = {}
    for proposal in decision.finding_proposals:
        identity = proposal.persistent_identity_bytes()
        links = frozenset(proposal.hypothesis_refs)
        previous_links = links_by_identity.get(identity)
        if previous_links is not None and previous_links != links:
            return DecisionValidationIssue(
                DecisionValidationCode.DUPLICATE_FINDING_IDENTITY_CONFLICTING_LINKS
            )
        links_by_identity[identity] = links
    return None


def _reject_disallowed_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, bytes | bytearray | memoryview):
        raise ValueError(f"{path} cannot contain bytes")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SECRET_FIELD.search(str(key)):
                raise ValueError(f"{path}.{key} may contain a secret")
            _reject_disallowed_payload(item, f"{path}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _reject_disallowed_payload(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SIGNED_URL.search(value):
        raise ValueError(f"{path} cannot contain a signed URL")


def parse_investigation_context(payload: str | bytes | Mapping[str, Any]) -> InvestigationContextV1:
    """Parse forward-compatible JSON while enforcing known fields and size bounds."""
    try:
        if isinstance(payload, (str, bytes)):
            raw = payload
        else:
            _reject_disallowed_payload(payload)
            raw = json.dumps(payload, default=str)
        return InvestigationContextV1.model_validate_json(raw, strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid investigation context: {exc}") from exc


def parse_agent_turn_decision(
    payload: str | bytes | Mapping[str, Any],
    *,
    tool_schemas: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> AgentTurnDecisionV1:
    """Read compact Bridge output and the persisted v1 JSON form without changing its model."""
    try:
        if isinstance(payload, (str, bytes)):
            raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        else:
            _reject_disallowed_payload(payload)
            raw = json.dumps(payload, ensure_ascii=False, default=str)
        if len(raw.encode("utf-8")) > MAX_DECISION_BYTES:
            raise ValueError(f"decision output exceeds {MAX_DECISION_BYTES} bytes")
        json_candidate = _agent_turn_json_candidate(raw)
        if json_candidate is not None:
            _reject_disallowed_payload(json_candidate)
            return AgentTurnDecisionV1.model_validate_json(
                json.dumps(json_candidate, ensure_ascii=False), strict=True
            )
        return _parse_agent_turn_wire(raw, tool_schemas=tool_schemas)
    except (UnicodeDecodeError, ValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Invalid agent turn decision:"):
            raise
        if isinstance(exc, ValidationError):
            issues = exc.errors(include_input=False, include_url=False)
            first = issues[0] if issues else {"loc": (), "msg": "contract validation failed"}
            location = ".".join(str(part) for part in first.get("loc", ())) or "decision"
            message = f"{location}: {first.get('msg', 'contract validation failed')}"
        else:
            message = str(exc) or type(exc).__name__
        raise ValueError(f"Invalid agent turn decision: {message[:256]}") from exc


@dataclass(slots=True)
class _DecisionWireBlock:
    kind: str
    start_line: int
    fields: dict[str, list[tuple[int, str]]]

    def add(self, key: str, line: int, value: str) -> None:
        self.fields.setdefault(key, []).append((line, value))


def _agent_turn_json_candidate(raw: str) -> dict[str, Any] | None:
    """Return an unfenced legacy object, if the complete response is JSON."""
    lines = [line for line in raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()]
    # Remove fence marker lines while leaving a JSON code block's contents intact.
    content = "\n".join(line for line in lines if not _WIRE_FENCE.fullmatch(line)).strip()
    if not content.startswith("{"):
        return None
    try:
        candidate = json.loads(content)
    except json.JSONDecodeError:
        return None
    return candidate if isinstance(candidate, dict) else None


def _wire_field_name(kind: str, name: str) -> str | None:
    normalized = re.sub(r"[\s-]+", "_", name.strip().casefold())
    aliases = {
        "DECISION": {
            "goal": "goal",
            "summary": "reasoning_summary",
            "reasoning_summary": "reasoning_summary",
            "next": "next_action",
            "next_action": "next_action",
            "hypotheses": "hypothesis_refs",
            "hypothesis_refs": "hypothesis_refs",
            "addressed_requests": "addressed_request_ids",
            "addressed_request_ids": "addressed_request_ids",
        },
        "TOOL": {
            "name": "tool_name",
            "tool_name": "tool_name",
            "version": "schema_version",
            "schema_version": "schema_version",
            "purpose": "purpose",
            "arguments": "arguments_json",
        },
        "FINDING": {
            "kind": "kind",
            "assessment": "assessment",
            "statement": "statement",
            "subject_ref": "subject_ref",
            "observations": "observation_refs",
            "observation_refs": "observation_refs",
            "claims": "claim_refs",
            "claim_refs": "claim_refs",
            "hypotheses": "hypothesis_refs",
            "hypothesis_refs": "hypothesis_refs",
            "novelty": "novelty",
            "scope": "scope",
            "limitations": "limitations",
        },
        "OPINION": {
            "type": "block_type",
            "block_type": "block_type",
            "text": "text",
            "findings": "finding_refs",
            "finding_refs": "finding_refs",
            "evidence": "evidence_refs",
            "evidence_refs": "evidence_refs",
            "qualification": "qualification",
            "certainty": "certainty",
            "unresolved": "unresolved_points",
            "unresolved_points": "unresolved_points",
        },
        "NOTE": {
            "anchor": "anchor",
            "state": "state",
            "relation": "relation",
            "text": "text",
            "findings": "finding_refs",
            "finding_refs": "finding_refs",
            "evidence": "evidence_refs",
            "evidence_refs": "evidence_refs",
        },
    }
    return aliases.get(kind, {}).get(normalized)


def _parse_agent_turn_wire(
    raw: str,
    *,
    tool_schemas: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> AgentTurnDecisionV1:
    blocks: list[_DecisionWireBlock] = []
    current: _DecisionWireBlock | None = None
    continuation_key: str | None = None
    decision_count = 0
    warning_codes: list[str] = []

    def warn(code: str, line: int) -> None:
        if len(warning_codes) < _MAX_WIRE_WARNINGS:
            warning_codes.append(f"{code}:line={line}")

    def finish() -> None:
        nonlocal current, continuation_key
        if current is not None:
            blocks.append(current)
        current = None
        continuation_key = None

    normalized_lines = raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    for line_number, line in enumerate(normalized_lines, 1):
        pending_json = _pending_json_argument(current, continuation_key)
        if pending_json is not None:
            argument_key, argument_line, prior_value = pending_json
            assert current is not None and continuation_key is not None
            tool_name = current.fields.get("tool_name", [(0, "TOOL")])[0][1].strip()
            tool_label = f"TOOL {tool_name or 'unknown'}"
            marker = _WIRE_MARKER.fullmatch(line)
            if marker is not None or line.lstrip().startswith("@@"):
                raise ValueError(
                    f"line {argument_line}: {tool_label}.ARG {argument_key} "
                    "JSON value is not closed "
                    f"before protocol marker at line {line_number}"
                )
            if _WIRE_ARG.fullmatch(line):
                raise ValueError(
                    f"line {argument_line}: {tool_label}.ARG {argument_key} "
                    "JSON value is not closed "
                    f"before the next ARG at line {line_number}"
                )
            values = current.fields[continuation_key]
            if line.strip():
                continued = f"{prior_value}\n{line.strip()}"
            else:
                continued = f"{prior_value}\n"
            if len(continued.encode("utf-8")) > _MAX_WIRE_JSON_ARGUMENT_BYTES:
                raise ValueError(
                    f"line {argument_line}: {tool_label}.ARG {argument_key} JSON value exceeds "
                    f"{_MAX_WIRE_JSON_ARGUMENT_BYTES} bytes"
                )
            if continued.count("\n") + 1 > _MAX_WIRE_JSON_ARGUMENT_LINES:
                raise ValueError(
                    f"line {argument_line}: {tool_label}.ARG {argument_key} JSON value exceeds "
                    f"{_MAX_WIRE_JSON_ARGUMENT_LINES} lines"
                )
            values[-1] = (argument_line, continued)
            continue
        if _WIRE_FENCE.fullmatch(line):
            continue
        marker = _WIRE_MARKER.fullmatch(line)
        if marker is not None:
            finish()
            kind = marker.group(1).upper()
            if kind == "END":
                continue
            if kind not in {"DECISION", "TOOL", "FINDING", "OPINION", "NOTE"}:
                continue
            if kind == "DECISION":
                decision_count += 1
            current = _DecisionWireBlock(kind, line_number, {})
            continue
        if current is None:
            continue
        argument = _WIRE_ARG.fullmatch(line) if current.kind == "TOOL" else None
        if argument is not None:
            argument_name = argument.group(1).strip()
            if argument_name:
                continuation_key = f"arg:{argument_name}"
                value = argument.group(2)
                if value.lstrip().startswith(("{", "[")):
                    if len(value.encode("utf-8")) > _MAX_WIRE_JSON_ARGUMENT_BYTES:
                        tool_name = current.fields.get("tool_name", [(0, "unknown")])[0][1]
                        tool_name = tool_name.strip()
                        raise ValueError(
                            f"line {line_number}: TOOL {tool_name or 'unknown'}."
                            f"ARG {argument_name} JSON value exceeds "
                            f"{_MAX_WIRE_JSON_ARGUMENT_BYTES} bytes"
                        )
                current.add(continuation_key, line_number, value)
            else:
                continuation_key = None
            continue
        field = _WIRE_FIELD.fullmatch(line)
        if field is not None:
            key = _wire_field_name(current.kind, field.group(1))
            if key is None:
                if current.kind == "NOTE":
                    raise ValueError(f"line {line_number}: unknown NOTE field")
                continuation_key = None
                continue
            continuation_key = key
            current.add(key, line_number, field.group(2))
            continue
        if continuation_key is not None and line.strip():
            values = current.fields[continuation_key]
            prior_line, prior_value = values[-1]
            values[-1] = (prior_line, f"{prior_value}\n{line.strip()}")

    pending_json = _pending_json_argument(current, continuation_key)
    if pending_json is not None:
        argument_key, argument_line, _ = pending_json
        tool_name = current.fields.get("tool_name", [(0, "TOOL")])[0][1].strip() if current else ""
        raise ValueError(
            f"line {argument_line}: TOOL {tool_name or 'unknown'}.ARG {argument_key} "
            "JSON value is not closed "
            "before end of output"
        )
    finish()
    if decision_count > 1:
        raise ValueError("Invalid agent turn decision: duplicate @@DECISION@@ block")
    decision_blocks = [block for block in blocks if block.kind == "DECISION"]
    if not decision_blocks:
        raise ValueError("Invalid agent turn decision: missing @@DECISION@@ block")
    decision_block = decision_blocks[0]
    _validate_wire_source_safety(blocks)

    def one(block: _DecisionWireBlock, key: str) -> tuple[int, str] | None:
        values = block.fields.get(key, [])
        if not values:
            return None
        if len(values) > 1:
            raise ValueError(f"line {values[1][0]}: duplicate {block.kind}.{key.upper()}")
        return values[0]

    def required_decision(key: str, label: str, maximum: int) -> str:
        item = one(decision_block, key)
        if item is None or not item[1].strip():
            raise ValueError(
                f"line {item[0] if item else decision_block.start_line}: missing DECISION.{label}"
            )
        if len(item[1].strip()) > maximum:
            raise ValueError(f"line {item[0]}: DECISION.{label} exceeds {maximum} characters")
        return item[1].strip()

    goal = required_decision("goal", "GOAL", 2_000)
    summary = required_decision("reasoning_summary", "SUMMARY", 8_000)
    next_item = one(decision_block, "next_action")
    if next_item is None or not next_item[1].strip():
        line_no = next_item[0] if next_item else decision_block.start_line
        raise ValueError(f"line {line_no}: missing DECISION.NEXT")
    next_value = next_item[1].strip().casefold()
    try:
        next_action = AgentNextAction(next_value)
    except ValueError as exc:
        raise ValueError(f"line {next_item[0]}: DECISION.NEXT value is unknown") from exc

    hypothesis_refs: list[str] = []
    for line_no, value in decision_block.fields.get("hypothesis_refs", []):
        try:
            hypothesis_refs.extend(_wire_list(value))
        except ValueError:
            warn("agent_turn_optional_field_ignored:decision.hypotheses", line_no)
    if len(hypothesis_refs) > 128:
        warn("agent_turn_optional_values_capped:decision.hypotheses", decision_block.start_line)
        hypothesis_refs = hypothesis_refs[:128]

    addressed_request_ids: list[UUID] = []
    for line_no, value in decision_block.fields.get("addressed_request_ids", []):
        try:
            addressed_request_ids.extend(UUID(item) for item in _wire_list(value))
        except (ValueError, TypeError):
            warn("agent_turn_optional_field_ignored:decision.addressed_requests", line_no)
            addressed_request_ids = []
            break
    addressed_request_ids = list(dict.fromkeys(addressed_request_ids))
    if len(addressed_request_ids) > 32:
        warn(
            "agent_turn_optional_values_capped:decision.addressed_requests",
            decision_block.start_line,
        )
        addressed_request_ids = addressed_request_ids[:32]

    tool_calls: list[ProposedToolCallV1] = []
    finding_proposals: list[FindingProposalV1] = []
    opinion_blocks: list[OpinionBlockDeltaV1] = []
    note_blocks: list[OpinionNoteDeltaV1] = []
    unresolved_points: list[str] = []
    opinion_seen = False
    note_seen = False
    opinion_block_types: set[OpinionBlockType] = set()

    for block in blocks:
        if block.kind == "TOOL":
            name = one(block, "tool_name")
            if name is None or not name[1].strip():
                raise ValueError(f"line {block.start_line}: missing TOOL.NAME")
            try:
                call = _tool_call_from_wire(block, tool_schemas=tool_schemas)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ValueError(
                    f"line {block.start_line}: TOOL {name[1].strip()} block rejected: "
                    f"{_wire_error_detail(exc)}"
                ) from exc
            if len(tool_calls) >= 16:
                raise ValueError(
                    f"line {block.start_line}: at most 16 TOOL blocks are allowed; remove extras"
                )
            tool_calls.append(call)
        elif block.kind == "FINDING":
            try:
                proposal = _finding_from_wire(block)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ValueError(
                    f"line {block.start_line}: FINDING block rejected: {_wire_error_detail(exc)}"
                ) from exc
            if len(finding_proposals) >= 128:
                raise ValueError(
                    f"line {block.start_line}: at most 128 FINDING blocks are allowed; "
                    "remove extras"
                )
            finding_proposals.append(proposal)
        elif block.kind == "OPINION":
            opinion_seen = True
            try:
                opinion, unresolved = _opinion_from_wire(block, warn)
            except (TypeError, ValueError, ValidationError) as exc:
                # A rejected opinion cannot be treated like an optional dropped block:
                # the Bridge loop must return a targeted correction before any tools run.
                detail = str(exc)
                if not re.match(r"^line \d+:", detail):
                    detail = f"line {block.start_line}: {detail}"
                raise ValueError(detail) from exc
            unresolved_points.extend(unresolved)
            if opinion is not None:
                if opinion.block_type in opinion_block_types:
                    raise ValueError(
                        f"line {block.start_line}: duplicate OPINION.TYPE; emit one block per type"
                    )
                if len(opinion_blocks) < 7:
                    opinion_blocks.append(opinion)
                    opinion_block_types.add(opinion.block_type)
                else:
                    raise ValueError(
                        f"line {block.start_line}: at most 7 OPINION blocks are allowed"
                    )
        elif block.kind == "NOTE":
            note_seen = True
            try:
                note = _note_from_wire(block)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ValueError(
                    f"line {block.start_line}: NOTE block rejected: {_wire_error_detail(exc)}"
                ) from exc
            if len(note_blocks) >= 24:
                raise ValueError(f"line {block.start_line}: at most 24 NOTE blocks are allowed")
            note_blocks.append(note)

    opinion_delta: OpinionDeltaV1 | None = None
    if opinion_seen or note_seen:
        opinion_delta = OpinionDeltaV1(
            blocks=tuple(opinion_blocks),
            notes=tuple(note_blocks),
            unresolved_points=tuple(unresolved_points[:128]),
        )
        if len(unresolved_points) > 128:
            warn("agent_turn_optional_values_capped:opinion.unresolved", decision_block.start_line)

    payload = {
        "reasoning_summary": summary,
        "goal": goal,
        "hypothesis_refs": tuple(hypothesis_refs),
        "addressed_request_ids": tuple(addressed_request_ids),
        "proposed_tool_calls": tuple(tool_calls),
        "finding_proposals": tuple(finding_proposals),
        "opinion_delta": opinion_delta,
        "next_action": next_action,
    }
    _reject_disallowed_payload(payload)
    try:
        decision = AgentTurnDecisionV1.model_validate(payload, strict=True)
    except ValidationError:
        raise
    if warning_codes:
        _logger.warning("agent_turn_wire_warnings=%s", ",".join(warning_codes[:_MAX_WIRE_WARNINGS]))
    return decision


def _pending_json_argument(
    block: _DecisionWireBlock | None,
    continuation_key: str | None,
) -> tuple[str, int, str] | None:
    if (
        block is None
        or block.kind != "TOOL"
        or continuation_key is None
        or not continuation_key.startswith("arg:")
    ):
        return None
    values = block.fields.get(continuation_key, [])
    if not values:
        return None
    line_number, value = values[-1]
    if _json_container_is_open(value):
        return continuation_key[4:], line_number, value
    return None


def _json_container_is_open(value: str) -> bool:
    raw = value.lstrip()
    if not raw.startswith(("{", "[")):
        return False
    stack: list[str] = []
    in_string = False
    escaped = False
    matching = {"}": "{", "]": "["}
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack or stack[-1] != matching[char]:
                # Keep treating malformed containers as pending until a protocol
                # boundary so their contents cannot be accepted as another field.
                return True
            stack.pop()
    return bool(stack) or in_string


def _wire_error_detail(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        issues = exc.errors(include_input=False, include_url=False)
        first = issues[0] if issues else {"loc": (), "msg": "contract validation failed"}
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "contract validation failed"))
        detail = f"{location}: {message}" if location else message
    else:
        detail = str(exc) or type(exc).__name__
    return " ".join(detail.split())[:180]


def _note_from_wire(block: _DecisionWireBlock) -> OpinionNoteDeltaV1:
    required = (
        "anchor",
        "state",
        "relation",
        "text",
        "finding_refs",
        "evidence_refs",
    )
    values: dict[str, str] = {}
    for key in required:
        items = block.fields.get(key, [])
        if len(items) != 1:
            raise ValueError(f"line {block.start_line}: NOTE requires exactly one {key.upper()}")
        values[key] = items[0][1].strip()
    try:
        findings = tuple(UUID(item) for item in _wire_uuid_list(values["finding_refs"]))
        evidence = tuple(UUID(item) for item in _wire_uuid_list(values["evidence_refs"]))
        if len(findings) > 16 or len(evidence) > 16:
            raise ValueError("NOTE reference list exceeds 16 entries")
        return OpinionNoteDeltaV1(
            anchor=values["anchor"],
            state=AnalystObservationState(values["state"].casefold()),
            relation=AnalystObservationRelation(values["relation"].casefold()),
            text=values["text"],
            finding_refs=findings,
            evidence_refs=evidence,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError(f"line {block.start_line}: NOTE reference or field is invalid") from exc


def _validate_wire_source_safety(blocks: list[_DecisionWireBlock]) -> None:
    raw_payload: dict[str, Any] = {}
    for block_index, block in enumerate(blocks):
        fields: dict[str, Any] = {}
        for key, values in block.fields.items():
            raw_values = [value for _, value in values]
            if key.startswith("arg:"):
                argument_value: Any = raw_values
                if len(raw_values) == 1 and raw_values[0].strip().startswith(("{", "[")):
                    try:
                        argument_value = json.loads(raw_values[0])
                    except json.JSONDecodeError:
                        pass
                fields.setdefault("arguments", {})[key[4:].strip()] = argument_value
            elif key == "arguments_json":
                raw = "\n".join(raw_values)
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    decoded = raw
                fields["arguments"] = decoded
            else:
                fields[key] = raw_values
        raw_payload[f"block_{block_index}"] = fields
    _reject_disallowed_payload(raw_payload)


def _wire_list(value: str) -> list[str]:
    raw = value.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return parsed
        raw = raw[1:-1].strip()
    parts: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if quote is not None and char == "\\":
            escaped = True
            continue
        if char in {'"', "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if quote is None and char in {",", ";", "\n"}:
            parts.append(raw[start:index].strip())
            start = index + 1
    parts.append(raw[start:].strip())
    values: list[str] = []
    for part in parts:
        if not part:
            continue
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}:
            if part[0] == '"':
                try:
                    decoded = json.loads(part)
                except json.JSONDecodeError:
                    decoded = part[1:-1]
            else:
                decoded = part[1:-1].replace("\\'", "'")
            values.append(decoded)
        else:
            values.append(part)
    return values


def _wire_uuid_list(value: str) -> list[str]:
    """Parse UUID references separated by commas, semicolons, or whitespace."""
    raw = value.strip()
    if not raw:
        return []
    parts = _wire_list(raw)
    return [token for part in parts for token in part.split() if token]


def _wire_enum(value: str, enum_type: type[Any]) -> Any:
    normalized = re.sub(r"[\s-]+", "_", value.strip().casefold())
    return next((member for member in enum_type if member.value.casefold() == normalized), None)


def _tool_call_from_wire(
    block: _DecisionWireBlock,
    *,
    tool_schemas: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> ProposedToolCallV1:
    name = block.fields["tool_name"][0]
    version = block.fields.get("schema_version", [])
    purpose = block.fields.get("purpose", [])
    if len(version) != 1 or not version[0][1].strip():
        raise ValueError("tool schema version is missing or duplicated")
    if len(purpose) != 1 or not purpose[0][1].strip():
        raise ValueError("tool purpose is missing or duplicated")
    if (
        len(name[1].strip()) > 128
        or len(version[0][1].strip()) > 64
        or len(purpose[0][1].strip()) > 2_000
    ):
        raise ValueError("tool field exceeds its bound")
    schema = (tool_schemas or {}).get((name[1].strip(), version[0][1].strip()))
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    if not isinstance(properties, Mapping):
        properties = {}
    arguments: dict[str, Any] = {}
    raw_arguments = block.fields.get("arguments_json", [])
    if raw_arguments:
        if len(raw_arguments) != 1:
            raise ValueError("tool arguments are duplicated")
        candidate = json.loads(raw_arguments[0][1])
        if not isinstance(candidate, dict):
            raise ValueError("tool arguments must be an object")
        arguments.update(candidate)
    for key, values in block.fields.items():
        if not key.startswith("arg:"):
            continue
        argument_name = key[4:].strip()
        if not argument_name or len(values) != 1:
            raise ValueError("tool argument is missing or duplicated")
        _set_dotted_wire_argument(arguments, argument_name, values[0][1], properties)
    # The tool block already carries a PURPOSE line; tools whose argument schema
    # also requires a ``purpose`` argument inherit it instead of failing validation.
    purpose_schema = properties.get("purpose")
    if (
        "purpose" not in arguments
        and isinstance(purpose_schema, Mapping)
        and purpose_schema.get("type") == "string"
    ):
        arguments["purpose"] = purpose[0][1].strip()
    return ProposedToolCallV1(
        tool_name=name[1].strip(),
        schema_version=version[0][1].strip(),
        arguments=arguments,
        purpose=purpose[0][1].strip(),
    )


def _wire_string_argument(value: str) -> Any:
    """Keep plain string content intact; parse only explicit quoted JSON strings."""
    raw = value.strip()
    if raw.startswith('"'):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        return parsed
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("\\'", "'")
    if raw.startswith(("{", "[")):
        # Explicit structured JSON remains structured and will be rejected by the
        # real tool schema when a string was expected.
        return _wire_argument(raw)
    return raw


def _wire_argument(value: str) -> Any:
    raw = value.strip()
    if not raw:
        return ""
    if raw.startswith(("{", "[")):
        return json.loads(raw)
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # A quoted list item can be followed by another comma-separated item.
            pass
    if raw.casefold() in {"true", "false", "null"} or re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    values = _wire_list(raw)
    if len(values) > 1:
        return values
    return raw


def _set_dotted_wire_argument(
    arguments: dict[str, Any],
    name: str,
    value: str,
    properties: Mapping[str, Any],
) -> None:
    """Build nested object arguments from dotted ARG names and plain scalar leaves."""
    parts = [item.strip() for item in name.split(".")]
    if not parts or any(not item for item in parts):
        raise ValueError("tool argument path is invalid")
    target = arguments
    schema_properties: Mapping[str, Any] = properties
    for part in parts[:-1]:
        schema = schema_properties.get(part)
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            raise ValueError("nested tool argument path is not an object field")
        nested_properties = schema.get("properties", {})
        if not isinstance(nested_properties, Mapping):
            raise ValueError("nested tool argument schema is invalid")
        current = target.get(part)
        if current is None:
            current = {}
            target[part] = current
        if not isinstance(current, dict):
            raise ValueError("nested tool argument path conflicts with a scalar")
        target = current
        schema_properties = nested_properties
    leaf = parts[-1]
    if leaf in target:
        raise ValueError("tool argument is duplicated")
    leaf_schema = schema_properties.get(leaf)
    if isinstance(leaf_schema, Mapping) and leaf_schema.get("type") == "string":
        target[leaf] = _wire_string_argument(value)
    else:
        target[leaf] = _wire_argument(value)


def _finding_from_wire(
    block: _DecisionWireBlock,
) -> FindingProposalV1:
    kind_item = block.fields.get("kind", [])
    assessment_item = block.fields.get("assessment", [])
    statement_item = block.fields.get("statement", [])
    kind = _wire_enum(kind_item[0][1], FindingKind) if len(kind_item) == 1 else None
    assessment = (
        _wire_enum(assessment_item[0][1], FindingAssessment) if len(assessment_item) == 1 else None
    )
    if kind is None:
        raise ValueError("FINDING.KIND is missing or unknown")
    if assessment is None:
        raise ValueError("FINDING.ASSESSMENT is missing or unknown")
    if len(statement_item) != 1:
        raise ValueError("FINDING.STATEMENT is missing or duplicated")
    statement = statement_item[0][1].strip()
    if not statement or len(statement) > 8_000:
        raise ValueError("FINDING.STATEMENT must contain 1 to 8000 characters")

    def optional_list(key: str, limit: int, *, uuids: bool = False) -> tuple[Any, ...]:
        values = block.fields.get(key, [])
        if not values:
            return ()
        if len(values) != 1:
            raise ValueError(f"FINDING.{key.upper()} is duplicated")
        try:
            items = _wire_uuid_list(values[0][1]) if uuids else _wire_list(values[0][1])
            parsed = tuple(UUID(item) for item in items) if uuids else tuple(items)
            if len(parsed) > limit:
                raise ValueError(f"FINDING.{key.upper()} exceeds {limit} entries")
            return parsed
        except (TypeError, ValueError) as exc:
            raise ValueError(f"FINDING.{key.upper()} contains an invalid list") from exc

    subject_values = block.fields.get("subject_ref", [])
    subject_ref = subject_values[0][1].strip() if len(subject_values) == 1 else None
    if len(subject_values) > 1:
        raise ValueError("FINDING.SUBJECT_REF is duplicated")
    if subject_ref is not None and (not subject_ref or len(subject_ref) > 256):
        raise ValueError("FINDING.SUBJECT_REF is invalid")
    novelty_values = block.fields.get("novelty", [])
    novelty = _wire_enum(novelty_values[0][1], NoveltyClass) if len(novelty_values) == 1 else None
    if novelty_values and (novelty is None or len(novelty_values) > 1):
        raise ValueError("FINDING.NOVELTY is unknown or duplicated")
    scope_values = block.fields.get("scope", [])
    scope = scope_values[0][1].strip() if len(scope_values) == 1 else ""
    if len(scope_values) > 1:
        raise ValueError("FINDING.SCOPE is duplicated")
    if len(scope) > 2_000:
        raise ValueError("FINDING.SCOPE exceeds 2000 characters")
    raw_limitations = block.fields.get("limitations", [])
    limitations: tuple[str, ...] = ()
    if raw_limitations:
        try:
            parsed_limitations = tuple(
                item for value in raw_limitations for item in _wire_list(value[1])
            )
            if len(parsed_limitations) > 128:
                raise ValueError("FINDING.LIMITATIONS exceeds 128 entries")
            limitations = parsed_limitations
        except ValueError as exc:
            raise ValueError("FINDING.LIMITATIONS contains an invalid list") from exc
    return FindingProposalV1(
        kind=kind,
        assessment=assessment,
        statement=statement,
        subject_ref=subject_ref,
        observation_refs=optional_list("observation_refs", 128, uuids=True),
        claim_refs=optional_list("claim_refs", 128),
        hypothesis_refs=optional_list("hypothesis_refs", 16),
        novelty=novelty,
        scope=scope,
        limitations=limitations,
    )


def _opinion_from_wire(
    block: _DecisionWireBlock,
    warn: Any,
) -> tuple[OpinionBlockDeltaV1 | None, tuple[str, ...]]:
    unresolved = list(_opinion_unresolved_from_wire(block, warn))
    type_values = block.fields.get("block_type", [])
    text_values = block.fields.get("text", [])
    if not type_values and not text_values:
        if unresolved:
            return None, tuple(unresolved[:128])
        raise ValueError("OPINION.TYPE and OPINION.TEXT are required")
    if len(type_values) != 1:
        raise ValueError("OPINION.TYPE requires exactly one known block type")
    block_type = _wire_enum(type_values[0][1], OpinionBlockType)
    if block_type is None or block_type in {
        OpinionBlockType.ANALYST_OBSERVATIONS,
        OpinionBlockType.IMPLANT_MATERIALS,
    }:
        raise ValueError("OPINION.TYPE must name a supported opinion block type")
    qualification_values = block.fields.get("qualification", [])
    qualification = (
        _wire_enum(qualification_values[0][1], OpinionClaimQualification)
        if len(qualification_values) == 1
        else None
    )
    if qualification is None:
        raise ValueError("OPINION.QUALIFICATION must be fact, inference, or hypothesis")
    certainty_values = block.fields.get("certainty", [])
    if len(certainty_values) != 1:
        raise ValueError(f"line {block.start_line}: OPINION.CERTAINTY requires exactly one value")
    certainty_match = _OPINION_CERTAINTY_PREFIX.match(certainty_values[0][1])
    if certainty_match is None:
        raise ValueError(
            f"line {certainty_values[0][0]}: OPINION.CERTAINTY must be a number from 0.0 to 1.0"
        )
    certainty = float(certainty_match.group("number"))
    if not 0.0 <= certainty <= 1.0:
        raise ValueError(
            f"line {certainty_values[0][0]}: OPINION.CERTAINTY must be a number from 0.0 to 1.0"
        )
    text = text_values[0][1] if len(text_values) == 1 else None
    if text is None or not text.strip() or len(text) > 12_000:
        raise ValueError("OPINION.TEXT is required and must not exceed 12000 characters")

    def optional_refs(key: str) -> tuple[UUID, ...]:
        values = block.fields.get(key, [])
        if not values:
            return ()
        if len(values) != 1:
            raise ValueError(f"line {values[1][0]}: OPINION.{key.upper()} must appear exactly once")
        try:
            result = tuple(UUID(item) for item in _wire_uuid_list(values[0][1]))
            if len(result) > 128:
                raise ValueError(f"OPINION.{key.upper()} exceeds 128 references")
            return result
        except ValueError as exc:
            raise ValueError(
                f"line {values[0][0]}: OPINION.{key.upper()} contains an invalid UUID reference"
            ) from exc

    return (
        OpinionBlockDeltaV1(
            block_type=block_type,
            text=text,
            finding_refs=optional_refs("finding_refs"),
            evidence_refs=optional_refs("evidence_refs"),
            qualification=qualification,
            certainty=certainty,
        ),
        tuple(unresolved[:128]),
    )


def _opinion_unresolved_from_wire(
    block: _DecisionWireBlock,
    warn: Any,
) -> tuple[str, ...]:
    unresolved: list[str] = []
    for line_no, value in block.fields.get("unresolved_points", []):
        try:
            unresolved.extend(_wire_list(value))
        except ValueError:
            warn("agent_turn_optional_field_ignored:opinion.unresolved", line_no)
    return tuple(unresolved[:128])


__all__ = [
    "MAX_CONTEXT_BYTES",
    "MAX_DECISION_BYTES",
    "AgentTurnDecisionV1",
    "ArticleClaimV1",
    "ArticleContextV1",
    "ContextChangeV1",
    "ContextInvestigationV1",
    "DecisionValidationCode",
    "DecisionValidationIssue",
    "FindingProposalV1",
    "InvestigationContextV1",
    "InvestigationDeltaV1",
    "InvestigationOperatorContextV1",
    "InvestigationSourceV1",
    "OperatorToolInvocationRequestV1",
    "OperatorToolInvocationResponseV1",
    "OperatorToolInvocationViewV1",
    "OpinionDeltaV1",
    "ProposedToolCallV1",
    "ToolSummaryV1",
    "parse_agent_turn_decision",
    "parse_investigation_context",
    "validate_decision_hypothesis_references",
]

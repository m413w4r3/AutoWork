"""Bounded, versioned Pydantic contracts exchanged with the Bridge."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cti_app.application.investigation_tools import ToolEffect
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.investigation import (
    AgentNextAction,
    EvidenceKind,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    canonical_json_bytes,
)

MAX_CONTEXT_BYTES = 262_144
MAX_DECISION_BYTES = 131_072
MAX_SUMMARY_CHARS = 12_000
_SECRET_FIELD = re.compile(
    r"(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|bearer|"
    r"token|secret|password|authorization|credential|cookie)",
    re.I,
)
_SIGNED_URL = re.compile(r"[?&](?:x-amz-[^=]+|x-goog-[^=]+|signature|sig|access_token)=", re.I)


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


class ToolSummaryV1(_StrictContract):
    name: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=64)
    effect: ToolEffect


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
    findings: tuple[FindingSummaryV1, ...] = Field(max_length=256)
    sample_deltas: tuple[dict[str, Any], ...] = Field(max_length=512)
    rule_deltas: tuple[dict[str, Any], ...] = Field(max_length=128)
    previous_actions: tuple[dict[str, Any], ...] = Field(max_length=256)
    user_requests: tuple[InvestigationUserRequestV1, ...] = Field(default=(), max_length=32)
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


class InvestigationContextV1(_StrictContract):
    SCHEMA: ClassVar[Literal["investigation-context.v1"]] = "investigation-context.v1"

    schema_version: Literal["investigation-context.v1"] = Field(default=SCHEMA, alias="schema")
    view: Literal["full", "delta"]
    investigation_id: UUID
    source: InvestigationSourceV1
    article: ArticleContextV1 | None = None
    investigation: ContextInvestigationV1
    delta: InvestigationDeltaV1 | None = None
    available_tools: tuple[ToolSummaryV1, ...] = Field(max_length=256)

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


class OpinionBlockDeltaV1(_StrictContract):
    block_type: OpinionBlockType
    text: str = Field(max_length=12_000)
    finding_refs: tuple[UUID, ...] = Field(default=(), max_length=128)
    evidence_refs: tuple[UUID, ...] = Field(default=(), max_length=128)
    qualification: OpinionClaimQualification
    certainty: float = Field(ge=0.0, le=1.0)


class OpinionDeltaV1(_StrictContract):
    blocks: tuple[OpinionBlockDeltaV1, ...] = Field(max_length=7)
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


def parse_agent_turn_decision(payload: str | bytes | Mapping[str, Any]) -> AgentTurnDecisionV1:
    """Ignore unknown fields for forward-compatible reading; validate known fields strictly."""
    try:
        if isinstance(payload, (str, bytes)):
            raw = payload
        else:
            _reject_disallowed_payload(payload)
            raw = json.dumps(payload, default=str)
        return AgentTurnDecisionV1.model_validate_json(raw, strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid agent turn decision: {exc}") from exc


__all__ = [
    "MAX_CONTEXT_BYTES",
    "MAX_DECISION_BYTES",
    "AgentTurnDecisionV1",
    "ArticleClaimV1",
    "ArticleContextV1",
    "ContextChangeV1",
    "ContextInvestigationV1",
    "FindingProposalV1",
    "InvestigationContextV1",
    "InvestigationDeltaV1",
    "InvestigationSourceV1",
    "OpinionDeltaV1",
    "ProposedToolCallV1",
    "ToolSummaryV1",
    "parse_agent_turn_decision",
    "parse_investigation_context",
]

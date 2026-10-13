"""Immutable analyst-opinion records and pure citation validation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from cti_app.domain.investigation import (
    EvidenceObservation,
    FindingAssessment,
    InvestigationFinding,
    canonical_json_bytes,
)


class OpinionBlockType(StrEnum):
    KEY_FINDINGS = "key_findings"
    ADDITIONAL_SAMPLES = "additional_samples"
    INFECTION_CHAIN_AND_CAPABILITIES = "infection_chain_and_capabilities"
    NETWORK_AND_INFRASTRUCTURE = "network_and_infrastructure"
    CLAIM_VERIFICATION = "claim_verification"
    PROPOSED_DETECTIONS = "proposed_detections"
    UNCERTAINTIES_AND_NEXT_LEADS = "uncertainties_and_next_leads"


class OpinionClaimQualification(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"


class AnalystOpinionStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class CitationViolationCode(StrEnum):
    FINDING_NOT_FOUND = "finding_not_found"
    OBSERVATION_NOT_FOUND = "observation_not_found"
    FACT_WITHOUT_SUPPORTED_FINDING = "fact_without_supported_finding"
    SUPPORTED_FINDING_WITHOUT_OBSERVATION = "supported_finding_without_observation"


@dataclass(frozen=True, slots=True)
class OpinionBlock:
    block_type: OpinionBlockType
    text: str
    finding_refs: tuple[UUID, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()
    qualification: OpinionClaimQualification = OpinionClaimQualification.FACT
    certainty: float = 0.5

    def __post_init__(self) -> None:
        if not isinstance(self.block_type, OpinionBlockType):
            raise TypeError("block_type must use OpinionBlockType")
        if not isinstance(self.qualification, OpinionClaimQualification):
            raise TypeError("qualification must use OpinionClaimQualification")
        if isinstance(self.certainty, bool) or not 0.0 <= self.certainty <= 1.0:
            raise ValueError("Opinion certainty must be between 0 and 1")
        object.__setattr__(self, "finding_refs", tuple(self.finding_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    @property
    def is_renderable(self) -> bool:
        return bool(self.text.strip())


@dataclass(frozen=True, slots=True)
class CitationViolation:
    code: CitationViolationCode
    block_type: OpinionBlockType
    reference_id: UUID | None = None
    message: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalystOpinionV1:
    investigation_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    base_publication_artifact_id: UUID
    base_publication_sha256: str
    blocks: tuple[OpinionBlock, ...]
    author_id: str
    status: AnalystOpinionStatus = AnalystOpinionStatus.DRAFT
    unresolved_points: tuple[str, ...] = ()
    reviewer_id: str | None = None
    id: UUID = field(default_factory=uuid4)
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    content_sha256: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, AnalystOpinionStatus):
            raise TypeError("status must use AnalystOpinionStatus")
        _validate_sha256(self.synthesis_sha256, "synthesis_sha256")
        _validate_sha256(self.base_publication_sha256, "base_publication_sha256")
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if not self.author_id.strip():
            raise ValueError("Opinion author identity must not be empty")
        if (
            self.status in (AnalystOpinionStatus.APPROVED, AnalystOpinionStatus.REJECTED)
            and not (self.reviewer_id or "").strip()
        ):
            raise ValueError("Reviewed opinions require a reviewer identity")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "unresolved_points", tuple(self.unresolved_points))
        block_types = tuple(block.block_type for block in self.blocks)
        if len(block_types) != len(set(block_types)):
            raise ValueError("An opinion may contain each block type only once")
        expected = hashlib.sha256(canonical_json_bytes(self.content_payload())).hexdigest()
        if self.content_sha256 and self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match canonical opinion content")
        object.__setattr__(self, "content_sha256", expected)

    @property
    def renderable_blocks(self) -> tuple[OpinionBlock, ...]:
        return tuple(block for block in self.blocks if block.is_renderable)

    def content_payload(self) -> dict[str, object]:
        """Return the stable JSON projection whose bytes are covered by content_sha256."""
        return {
            "schema": "analyst-opinion.v1",
            "id": str(self.id),
            "version": self.version,
            "investigation_id": str(self.investigation_id),
            "synthesis_artifact_id": str(self.synthesis_artifact_id),
            "synthesis_sha256": self.synthesis_sha256,
            "base_publication_artifact_id": str(self.base_publication_artifact_id),
            "base_publication_sha256": self.base_publication_sha256,
            "blocks": [
                {
                    "type": block.block_type.value,
                    "text": block.text,
                    "finding_refs": [str(ref) for ref in block.finding_refs],
                    "evidence_refs": [str(ref) for ref in block.evidence_refs],
                    "qualification": block.qualification.value,
                    "certainty": block.certainty,
                }
                for block in self.blocks
            ],
            "unresolved_points": list(self.unresolved_points),
            "author_id": self.author_id,
            "reviewer_id": self.reviewer_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.content_payload())

    def to_payload(self) -> dict[str, object]:
        return {**self.content_payload(), "content_sha256": self.content_sha256}


def _validate_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase SHA-256")


def verify_opinion_citations(
    opinion: AnalystOpinionV1,
    findings: Iterable[InvestigationFinding],
    observations: Iterable[EvidenceObservation],
) -> tuple[CitationViolation, ...]:
    """Check all references and require strong factual blocks to cite supported evidence."""
    finding_by_id = {finding.id: finding for finding in findings}
    observation_ids = {observation.id for observation in observations}
    violations: list[CitationViolation] = []
    for block in opinion.blocks:
        referenced_findings: list[InvestigationFinding] = []
        for finding_ref in block.finding_refs:
            finding = finding_by_id.get(finding_ref)
            if finding is None:
                violations.append(
                    CitationViolation(
                        CitationViolationCode.FINDING_NOT_FOUND,
                        block.block_type,
                        finding_ref,
                        "The cited finding does not exist.",
                    )
                )
            else:
                referenced_findings.append(finding)
        for evidence_ref in block.evidence_refs:
            if evidence_ref not in observation_ids:
                violations.append(
                    CitationViolation(
                        CitationViolationCode.OBSERVATION_NOT_FOUND,
                        block.block_type,
                        evidence_ref,
                        "The cited observation does not exist.",
                    )
                )

        supported_with_evidence = [
            finding
            for finding in referenced_findings
            if finding.assessment is FindingAssessment.SUPPORTED
            and finding.observation_refs
            and all(ref in observation_ids for ref in finding.observation_refs)
        ]
        for finding in referenced_findings:
            if finding.assessment is FindingAssessment.SUPPORTED and (
                not finding.observation_refs
                or any(ref not in observation_ids for ref in finding.observation_refs)
            ):
                violations.append(
                    CitationViolation(
                        CitationViolationCode.SUPPORTED_FINDING_WITHOUT_OBSERVATION,
                        block.block_type,
                        finding.id,
                        "A supported finding cites no existing observation.",
                    )
                )
        if block.qualification is OpinionClaimQualification.FACT and not supported_with_evidence:
            violations.append(
                CitationViolation(
                    CitationViolationCode.FACT_WITHOUT_SUPPORTED_FINDING,
                    block.block_type,
                    None,
                    "A factual block requires a supported finding backed by existing observations.",
                )
            )
    return tuple(violations)


__all__ = [
    "AnalystOpinionStatus",
    "AnalystOpinionV1",
    "CitationViolation",
    "CitationViolationCode",
    "OpinionBlock",
    "OpinionBlockType",
    "OpinionClaimQualification",
    "verify_opinion_citations",
]

"""Immutable analyst-opinion records and pure citation validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.investigation import (
    EvidenceObservation,
    FindingAssessment,
    InvestigationFinding,
    canonical_json_bytes,
)
from cti_app.domain.network_protocol import StreamDirection
from cti_app.domain.protocol_model import (
    ProtocolDiagramKind,
    ProtocolEvidenceRefV1,
)


class OpinionBlockType(StrEnum):
    KEY_FINDINGS = "key_findings"
    ADDITIONAL_SAMPLES = "additional_samples"
    INFECTION_CHAIN_AND_CAPABILITIES = "infection_chain_and_capabilities"
    NETWORK_AND_INFRASTRUCTURE = "network_and_infrastructure"
    CLAIM_VERIFICATION = "claim_verification"
    PROPOSED_DETECTIONS = "proposed_detections"
    UNCERTAINTIES_AND_NEXT_LEADS = "uncertainties_and_next_leads"
    ANALYST_OBSERVATIONS = "analyst_observations"


class OpinionClaimQualification(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"


class AnalystObservationState(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"


class AnalystObservationRelation(StrEnum):
    CONFIRMS = "confirms"
    REFUTES = "refutes"
    QUALIFIES = "qualifies"
    UNRESOLVED = "unresolved"


class AnalystOpinionStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class CitationViolationCode(StrEnum):
    FINDING_NOT_FOUND = "finding_not_found"
    OBSERVATION_NOT_FOUND = "observation_not_found"
    FACT_WITHOUT_SUPPORTED_FINDING = "fact_without_supported_finding"
    SUPPORTED_FINDING_WITHOUT_OBSERVATION = "supported_finding_without_observation"
    OBSERVATION_NOT_SUPPORTED = "analyst_observation_not_supported"


@dataclass(frozen=True, slots=True)
class OpinionProtocolFieldEvidenceV1:
    """Bounded capture and frame provenance retained for one protocol field."""

    field_id: str
    evidence_refs: tuple[ProtocolEvidenceRefV1, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.field_id, str) or not self.field_id.strip():
            raise ValueError("Protocol field evidence requires a field ID")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("Protocol field evidence requires at least one reference")
        if len(self.evidence_refs) > 8:
            raise ValueError("Protocol field evidence is limited to eight capture references")
        if any(
            not isinstance(ref, ProtocolEvidenceRefV1) or len(ref.frame_numbers) > 12
            for ref in self.evidence_refs
        ):
            raise ValueError("Protocol field evidence references exceed their bounds")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("Protocol field evidence references must be unique")
        if not isinstance(self.truncated, bool):
            raise ValueError("Protocol field evidence truncation marker is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "truncated": self.truncated,
        }


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


_OPINION_ANCHOR = re.compile(r"^[a-z][a-z0-9:_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class AnalystObservationV1:
    """An analyst observation projected beside one stable V5 paragraph anchor."""

    id: UUID
    anchor: str
    text: str
    state: AnalystObservationState
    relation: AnalystObservationRelation
    finding_refs: tuple[UUID, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise ValueError("Analyst observation identity must be a UUID")
        if not isinstance(self.anchor, str) or _OPINION_ANCHOR.fullmatch(self.anchor) is None:
            raise ValueError("Analyst observation anchor is invalid")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Analyst observation text must be non-empty")
        if not isinstance(self.state, AnalystObservationState):
            raise TypeError("Analyst observation state must use AnalystObservationState")
        if not isinstance(self.relation, AnalystObservationRelation):
            raise TypeError("Analyst observation relation must use AnalystObservationRelation")
        object.__setattr__(self, "finding_refs", tuple(self.finding_refs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if any(not isinstance(item, UUID) for item in self.finding_refs + self.evidence_refs):
            raise ValueError("Analyst observation references must be UUIDs")
        if len(set(self.finding_refs)) != len(self.finding_refs) or len(
            set(self.evidence_refs)
        ) != len(self.evidence_refs):
            raise ValueError("Analyst observation references must be unique")
        if self.state in {
            AnalystObservationState.OBSERVED,
            AnalystObservationState.INFERRED,
        } and not (self.finding_refs or self.evidence_refs):
            raise ValueError("Sourced analyst observations require finding or evidence references")
        if self.state is AnalystObservationState.OBSERVED and not self.evidence_refs:
            raise ValueError("Observed analyst notes require explicit evidence references")

    def to_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "anchor": self.anchor,
            "text": self.text,
            "state": self.state.value,
            "relation": self.relation.value,
            "finding_refs": [str(ref) for ref in self.finding_refs],
            "evidence_refs": [str(ref) for ref in self.evidence_refs],
        }

    @classmethod
    def from_payload(cls, value: object) -> AnalystObservationV1:
        expected = {
            "id",
            "anchor",
            "text",
            "state",
            "relation",
            "finding_refs",
            "evidence_refs",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("Analyst observation payload has missing or unknown fields")
        if any(
            not isinstance(value[key], str) for key in ("id", "anchor", "text", "state", "relation")
        ):
            raise ValueError("Analyst observation text fields are invalid")
        finding_refs = value["finding_refs"]
        evidence_refs = value["evidence_refs"]
        if not isinstance(finding_refs, (list, tuple)) or not isinstance(
            evidence_refs, (list, tuple)
        ):
            raise ValueError("Analyst observation references must be lists")
        if any(not isinstance(item, str) for item in (*finding_refs, *evidence_refs)):
            raise ValueError("Analyst observation reference IDs must be strings")
        return cls(
            id=UUID(value["id"]),
            anchor=value["anchor"],
            text=value["text"],
            state=AnalystObservationState(value["state"]),
            relation=AnalystObservationRelation(value["relation"]),
            finding_refs=tuple(UUID(item) for item in finding_refs),
            evidence_refs=tuple(UUID(item) for item in evidence_refs),
        )


@dataclass(frozen=True, slots=True)
class OpinionProtocolFigureV1:
    """A version-pinned protocol view attached to the exact opinion content."""

    kind: ProtocolDiagramKind
    model_sha256: str
    spec_sha256: str
    asset_id: UUID
    asset_sha256: str
    preview_sha256: str
    pin_sha256: str
    compiler_name: str
    compiler_version: str
    compiler_policy_version: str
    caption: str
    field_evidence: tuple[OpinionProtocolFieldEvidenceV1, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProtocolDiagramKind):
            raise TypeError("Protocol figure kind must use ProtocolDiagramKind")
        for name in ("model_sha256", "spec_sha256", "asset_sha256", "preview_sha256", "pin_sha256"):
            _validate_sha256(getattr(self, name), name)
        if not isinstance(self.asset_id, UUID):
            raise ValueError("Protocol figure asset identity must be a UUID")
        for name in ("compiler_name", "compiler_version", "compiler_policy_version", "caption"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Protocol figure {name} must be non-empty text")
        if not isinstance(self.field_evidence, tuple) or any(
            not isinstance(item, OpinionProtocolFieldEvidenceV1) for item in self.field_evidence
        ):
            raise ValueError("Protocol figure field evidence metadata is invalid")
        if len({item.field_id for item in self.field_evidence}) != len(self.field_evidence):
            raise ValueError("Protocol figure field evidence IDs must be unique")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind.value,
            "model_sha256": self.model_sha256,
            "spec_sha256": self.spec_sha256,
            "asset_id": str(self.asset_id),
            "asset_sha256": self.asset_sha256,
            "preview_sha256": self.preview_sha256,
            "pin_sha256": self.pin_sha256,
            "compiler_name": self.compiler_name,
            "compiler_version": self.compiler_version,
            "compiler_policy_version": self.compiler_policy_version,
            "caption": self.caption,
        }
        if self.field_evidence:
            payload["field_evidence"] = [item.to_payload() for item in self.field_evidence]
        return payload

    @classmethod
    def from_payload(cls, value: object) -> OpinionProtocolFigureV1:
        expected = {
            "kind",
            "model_sha256",
            "spec_sha256",
            "asset_id",
            "asset_sha256",
            "preview_sha256",
            "pin_sha256",
            "compiler_name",
            "compiler_version",
            "compiler_policy_version",
            "caption",
        }
        if not isinstance(value, Mapping) or frozenset(value) not in {
            frozenset(expected),
            frozenset(expected | {"field_evidence"}),
        }:
            raise ValueError("Protocol figure payload has missing or unknown fields")
        field_evidence_payload = value.get("field_evidence", [])
        if not isinstance(field_evidence_payload, (list, tuple)):
            raise ValueError("Protocol figure field evidence metadata must be a list")
        return cls(
            kind=ProtocolDiagramKind(_payload_text(value, "kind")),
            model_sha256=_payload_text(value, "model_sha256"),
            spec_sha256=_payload_text(value, "spec_sha256"),
            asset_id=UUID(_payload_text(value, "asset_id")),
            asset_sha256=_payload_text(value, "asset_sha256"),
            preview_sha256=_payload_text(value, "preview_sha256"),
            pin_sha256=_payload_text(value, "pin_sha256"),
            compiler_name=_payload_text(value, "compiler_name"),
            compiler_version=_payload_text(value, "compiler_version"),
            compiler_policy_version=_payload_text(value, "compiler_policy_version"),
            caption=_payload_text(value, "caption"),
            field_evidence=tuple(
                _protocol_field_evidence_from_payload(item) for item in field_evidence_payload
            ),
        )


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
    protocol_figures: tuple[OpinionProtocolFigureV1, ...] = ()
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
        object.__setattr__(self, "protocol_figures", tuple(self.protocol_figures))
        if any(not isinstance(item, OpinionProtocolFigureV1) for item in self.protocol_figures):
            raise TypeError("Opinion protocol figures must contain OpinionProtocolFigureV1 values")
        figure_kinds = tuple(item.kind for item in self.protocol_figures)
        if figure_kinds and set(figure_kinds) != set(ProtocolDiagramKind):
            raise ValueError("An opinion must attach all three protocol views together")
        if len(figure_kinds) != len(set(figure_kinds)):
            raise ValueError("An opinion must not repeat a protocol figure kind")
        _ = self.observations
        block_types = tuple(block.block_type for block in self.blocks)
        if len(block_types) != len(set(block_types)):
            raise ValueError("An opinion may contain each block type only once")
        expected = hashlib.sha256(canonical_json_bytes(self.content_payload())).hexdigest()
        if self.content_sha256 and self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match canonical opinion content")
        object.__setattr__(self, "content_sha256", expected)

    @property
    def renderable_blocks(self) -> tuple[OpinionBlock, ...]:
        return tuple(
            block
            for block in self.blocks
            if block.is_renderable and block.block_type is not OpinionBlockType.ANALYST_OBSERVATIONS
        )

    @property
    def observations(self) -> tuple[AnalystObservationV1, ...]:
        payload_blocks = [
            block
            for block in self.blocks
            if block.block_type is OpinionBlockType.ANALYST_OBSERVATIONS
        ]
        if not payload_blocks:
            return ()
        if len(payload_blocks) != 1:
            raise ValueError("Opinion must contain one analyst-observation projection block")
        try:
            payload = json.loads(payload_blocks[0].text)
        except json.JSONDecodeError as exc:
            raise ValueError("Opinion analyst-observation projection is invalid JSON") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "items"}
            or payload["schema"] != "analyst-observations.v1"
            or not isinstance(payload["items"], list)
        ):
            raise ValueError("Opinion analyst-observation projection is unsupported")
        result = tuple(AnalystObservationV1.from_payload(item) for item in payload["items"])
        if not result or len({item.id for item in result}) != len(result):
            raise ValueError("Opinion analyst-observation identities must be unique and non-empty")
        if len(result) > 100:
            raise ValueError("An opinion is limited to 100 analyst observations")
        return result

    def content_payload(self) -> dict[str, object]:
        """Return the stable JSON projection whose bytes are covered by content_sha256."""
        payload: dict[str, object] = {
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
        if self.protocol_figures:
            payload["protocol_figures"] = [item.to_payload() for item in self.protocol_figures]
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.content_payload())

    def to_payload(self) -> dict[str, object]:
        return {**self.content_payload(), "content_sha256": self.content_sha256}


def _validate_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase SHA-256")


def opinion_blocks_with_observations(
    blocks: Iterable[OpinionBlock], observations: Iterable[AnalystObservationV1]
) -> tuple[OpinionBlock, ...]:
    """Persist the versioned note projection inside the existing immutable opinion payload."""
    ordinary_blocks = tuple(
        block for block in blocks if block.block_type is not OpinionBlockType.ANALYST_OBSERVATIONS
    )
    notes = tuple(observations)
    if not notes:
        return ordinary_blocks
    payload = {
        "schema": "analyst-observations.v1",
        "items": [item.to_payload() for item in notes],
    }
    projection = OpinionBlock(
        block_type=OpinionBlockType.ANALYST_OBSERVATIONS,
        text=canonical_json_bytes(payload).decode("utf-8"),
        finding_refs=(),
        evidence_refs=(),
        qualification=OpinionClaimQualification.HYPOTHESIS,
        certainty=0.0,
    )
    return (*ordinary_blocks, projection)


def _payload_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"Protocol figure {key} must be text")
    return value


def _protocol_field_evidence_from_payload(value: object) -> OpinionProtocolFieldEvidenceV1:
    if not isinstance(value, Mapping) or set(value) != {
        "field_id",
        "evidence_refs",
        "truncated",
    }:
        raise ValueError("Protocol field evidence payload has missing or unknown fields")
    field_id = value["field_id"]
    raw_refs = value["evidence_refs"]
    truncated = value["truncated"]
    if not isinstance(field_id, str):
        raise ValueError("Protocol field evidence ID must be text")
    if not isinstance(raw_refs, (list, tuple)):
        raise ValueError("Protocol field evidence references must be a list")
    if not isinstance(truncated, bool):
        raise ValueError("Protocol field evidence truncation marker is invalid")
    return OpinionProtocolFieldEvidenceV1(
        field_id=field_id,
        evidence_refs=tuple(_protocol_evidence_from_payload(item) for item in raw_refs),
        truncated=truncated,
    )


def _protocol_evidence_from_payload(value: object) -> ProtocolEvidenceRefV1:
    expected = {
        "capture_sha256",
        "flow_id",
        "stream_id",
        "direction",
        "frame_numbers",
        "start_offset",
        "end_offset",
        "source",
        "observation_id",
        "timestamp_start_ns",
        "timestamp_end_ns",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("Protocol evidence payload has missing or unknown fields")
    frames = value["frame_numbers"]
    if not isinstance(frames, (list, tuple)):
        raise ValueError("Protocol evidence frame numbers must be a list")

    def optional_int(raw: object) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("Protocol evidence numeric metadata is invalid")
        return raw

    text_fields = ("capture_sha256", "flow_id", "stream_id", "direction", "source")
    if any(not isinstance(value[name], str) for name in text_fields):
        raise ValueError("Protocol evidence text metadata is invalid")
    if any(isinstance(frame, bool) or not isinstance(frame, int) for frame in frames):
        raise ValueError("Protocol evidence frame numbers are invalid")
    observation_id = value["observation_id"]
    if observation_id is not None and not isinstance(observation_id, str):
        raise ValueError("Protocol evidence observation identity is invalid")
    return ProtocolEvidenceRefV1(
        capture_sha256=value["capture_sha256"],
        flow_id=value["flow_id"],
        stream_id=value["stream_id"],
        direction=StreamDirection(value["direction"]),
        frame_numbers=tuple(frames),
        start_offset=optional_int(value["start_offset"]),
        end_offset=optional_int(value["end_offset"]),
        source=value["source"],
        observation_id=UUID(observation_id) if observation_id is not None else None,
        timestamp_start_ns=optional_int(value["timestamp_start_ns"]),
        timestamp_end_ns=optional_int(value["timestamp_end_ns"]),
    )


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
    for note in opinion.observations:
        referenced_findings: list[InvestigationFinding] = []
        for finding_ref in note.finding_refs:
            finding = finding_by_id.get(finding_ref)
            if finding is None:
                violations.append(
                    CitationViolation(
                        CitationViolationCode.FINDING_NOT_FOUND,
                        OpinionBlockType.KEY_FINDINGS,
                        finding_ref,
                        "The cited analyst-observation finding does not exist.",
                    )
                )
            else:
                referenced_findings.append(finding)
        for evidence_ref in note.evidence_refs:
            if evidence_ref not in observation_ids:
                violations.append(
                    CitationViolation(
                        CitationViolationCode.OBSERVATION_NOT_FOUND,
                        OpinionBlockType.KEY_FINDINGS,
                        evidence_ref,
                        "The cited analyst-observation evidence does not exist.",
                    )
                )
        if note.state is AnalystObservationState.OBSERVED and not any(
            finding.assessment is FindingAssessment.SUPPORTED
            and finding.observation_refs
            and all(ref in observation_ids for ref in finding.observation_refs)
            for finding in referenced_findings
        ):
            violations.append(
                CitationViolation(
                    CitationViolationCode.OBSERVATION_NOT_SUPPORTED,
                    OpinionBlockType.KEY_FINDINGS,
                    note.id,
                    "An observed analyst note requires a supported finding backed by evidence.",
                )
            )
    return tuple(violations)


__all__ = [
    "AnalystObservationRelation",
    "AnalystObservationState",
    "AnalystObservationV1",
    "AnalystOpinionStatus",
    "AnalystOpinionV1",
    "CitationViolation",
    "CitationViolationCode",
    "OpinionBlock",
    "OpinionBlockType",
    "OpinionClaimQualification",
    "OpinionProtocolFieldEvidenceV1",
    "OpinionProtocolFigureV1",
    "verify_opinion_citations",
]

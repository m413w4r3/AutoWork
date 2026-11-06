"""Deterministic subject-level routing from canonical extraction artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid5

from cti_app.domain.collection import SourceAcquisitionMethod
from cti_app.domain.indicator_normalization import normalize_indicator_value
from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ProductionExtractionOmissionReason,
    ProductionExtractionV1,
    production_extraction_hashes,
)
from cti_app.domain.production_references import (
    PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
    ProductionReferenceTier,
    ProductionSourceCriticality,
    ProductionSourceCriticalityReason,
)
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceProjectionV1,
    validate_relevance_projection_lineage,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    extraction_evidence_elements,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.subject_ioc_eligibility import is_subject_ioc_eligible

ROUTING_POLICY_VERSION = "subject-publication-routing-v2-bridge-evidence-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DECISION_NAMESPACE = UUID("484713e4-12ca-5e67-b18a-cafdad6ee989")


class RoutingStatus(StrEnum):
    DECIDED = "decided"
    NEEDS_REVIEW = "needs_review"


class RoutingReasonCode(StrEnum):
    SUBJECT_IOC_CONFIRMED = "subject_ioc_confirmed"
    NO_SUBJECT_IOC = "no_subject_ioc"
    MISSING_EXTRACTION_ARTIFACT = "missing_extraction_artifact"
    MISSING_RELEVANCE_ARTIFACT = "missing_relevance_artifact"
    INVALID_EXTRACTION_ARTIFACT = "invalid_extraction_artifact"
    INVALID_RELEVANCE_ARTIFACT = "invalid_relevance_artifact"
    INCOMPLETE_EXTRACTION = "incomplete_extraction"
    INCOMPLETE_PROJECTION = "incomplete_projection"
    PROVENANCE_MISMATCH = "provenance_mismatch"
    CRITICAL_SOURCE_MISSING = "critical_source_missing"


@dataclass(frozen=True, slots=True)
class RoutingCriticalSourceDiagnosticV1:
    source_id: str | None
    source_url: str
    failure_code: str
    tier: str
    criticality: ProductionSourceCriticality
    criticality_reason: str
    policy_version: str

    def __post_init__(self) -> None:
        for name in (
            "source_url",
            "failure_code",
            "tier",
            "criticality_reason",
            "policy_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        if self.source_id is not None and not isinstance(self.source_id, str):
            raise ValueError("source_id must be text or None")
        if not isinstance(self.criticality, ProductionSourceCriticality):
            raise ValueError("criticality is invalid")
        if self.criticality_reason not in {
            item.value for item in ProductionSourceCriticalityReason
        }:
            raise ValueError("criticality_reason is invalid")
        if self.policy_version != PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION:
            raise ValueError("criticality policy version is unsupported")

    def to_json(self) -> dict[str, str | None]:
        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "failure_code": self.failure_code,
            "tier": self.tier,
            "criticality": self.criticality.value,
            "criticality_reason": self.criticality_reason,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class ProductionRoutingArtifactInputV1:
    """Verified artifact identity and decoded canonical value passed to the policy."""

    artifact: ProductionArtifact | None
    artifact_hash: str | None
    payload: ProductionExtractionV1 | RelevanceProjectionV1 | None
    validation_error: RoutingReasonCode | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionRoutingDecisionV1:
    decision_id: UUID
    production_run_id: UUID
    pipeline_generation: int
    extraction_artifact_id: UUID | None
    extraction_artifact_hash: str | None
    relevance_artifact_id: UUID | None
    relevance_artifact_hash: str | None
    input_hash: str
    policy_version: str
    status: RoutingStatus
    publication_kind: PublicationKind | None
    reason_code: RoutingReasonCode
    confirmed_ioc_count: int
    contextual_ioc_count: int
    ambiguous_ioc_count: int
    critical_source_diagnostics: tuple[RoutingCriticalSourceDiagnosticV1, ...]
    decided_at: datetime
    decision_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, UUID) or not isinstance(self.production_run_id, UUID):
            raise ValueError("Routing decision identities must be UUIDs")
        if not isinstance(self.status, RoutingStatus):
            raise ValueError("status is invalid")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty text")
        if type(self.pipeline_generation) is not int or self.pipeline_generation < 0:
            raise ValueError("pipeline_generation must be a non-negative integer")
        for label in ("input_hash", "decision_hash"):
            value = getattr(self, label)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{label} must be a lowercase SHA-256")
        for label in ("extraction_artifact_hash", "relevance_artifact_hash"):
            value = getattr(self, label)
            if value is not None and _SHA256.fullmatch(value) is None:
                raise ValueError(f"{label} must be a lowercase SHA-256 or None")
        if self.status is RoutingStatus.DECIDED and self.publication_kind is None:
            raise ValueError("A decided route requires a publication kind")
        if self.status is RoutingStatus.NEEDS_REVIEW and self.publication_kind is not None:
            raise ValueError("A route needing review cannot select a publication kind")
        if not isinstance(self.reason_code, RoutingReasonCode):
            raise ValueError("reason_code is invalid")
        if self.publication_kind is not None and not isinstance(
            self.publication_kind, PublicationKind
        ):
            raise ValueError("publication_kind is invalid")
        for label in ("confirmed_ioc_count", "contextual_ioc_count", "ambiguous_ioc_count"):
            value = getattr(self, label)
            if type(value) is not int or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if not isinstance(self.critical_source_diagnostics, tuple):
            raise ValueError("critical_source_diagnostics must be a tuple")
        if any(
            not isinstance(item, RoutingCriticalSourceDiagnosticV1)
            for item in self.critical_source_diagnostics
        ):
            raise ValueError("critical_source_diagnostics contains an invalid value")
        if not isinstance(self.decided_at, datetime):
            raise ValueError("decided_at must be a datetime")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _diagnostic_json(
    diagnostics: tuple[RoutingCriticalSourceDiagnosticV1, ...],
) -> list[dict[str, str | None]]:
    return [item.to_json() for item in diagnostics]


def _artifact_identity(value: ProductionRoutingArtifactInputV1) -> dict[str, str | None]:
    return {
        "artifact_id": str(value.artifact.id) if value.artifact is not None else None,
        "artifact_hash": value.artifact_hash,
    }


def _valid_artifact(
    *,
    value: ProductionRoutingArtifactInputV1,
    run_id: UUID,
    subject_id: UUID,
    stage: ProductionArtifactStage,
    payload_type: type[ProductionExtractionV1] | type[RelevanceProjectionV1],
    missing_reason: RoutingReasonCode,
    invalid_reason: RoutingReasonCode,
) -> RoutingReasonCode | None:
    artifact = value.artifact
    if artifact is None:
        return missing_reason
    if (
        artifact.production_run_id != run_id
        or artifact.subject_id != subject_id
        or artifact.stage is not stage
    ):
        return RoutingReasonCode.PROVENANCE_MISMATCH
    if value.validation_error is not None:
        return value.validation_error
    if artifact.status is not ProductionArtifactStatus.VERIFIED:
        return invalid_reason
    if value.payload is None or not isinstance(value.payload, payload_type):
        return invalid_reason
    if value.artifact_hash is None or _SHA256.fullmatch(value.artifact_hash) is None:
        return invalid_reason
    return None


def _decision_payload(
    *,
    production_run_id: UUID,
    pipeline_generation: int,
    extraction_input: ProductionRoutingArtifactInputV1,
    relevance_input: ProductionRoutingArtifactInputV1,
    input_hash: str,
    status: RoutingStatus,
    publication_kind: PublicationKind | None,
    reason_code: RoutingReasonCode,
    confirmed_ioc_count: int,
    contextual_ioc_count: int,
    ambiguous_ioc_count: int,
    diagnostics: tuple[RoutingCriticalSourceDiagnosticV1, ...],
) -> dict[str, object]:
    return {
        "production_run_id": str(production_run_id),
        "pipeline_generation": pipeline_generation,
        "extraction_artifact": _artifact_identity(extraction_input),
        "relevance_artifact": _artifact_identity(relevance_input),
        "input_hash": input_hash,
        "policy_version": ROUTING_POLICY_VERSION,
        "status": status.value,
        "publication_kind": publication_kind.value if publication_kind else None,
        "reason_code": reason_code.value,
        "confirmed_ioc_count": confirmed_ioc_count,
        "contextual_ioc_count": contextual_ioc_count,
        "ambiguous_ioc_count": ambiguous_ioc_count,
        "critical_source_diagnostics": _diagnostic_json(diagnostics),
    }


def decide_publication_routing(
    *,
    production_run_id: UUID,
    subject_id: UUID,
    pipeline_generation: int,
    extraction_input: ProductionRoutingArtifactInputV1,
    relevance_input: ProductionRoutingArtifactInputV1,
    legitimate_services: LegitimateServiceRegistry,
    decided_at: datetime,
    critical_source_diagnostics: tuple[RoutingCriticalSourceDiagnosticV1, ...] = (),
) -> ProductionRoutingDecisionV1:
    """Choose ARTICLE, BRIEF, or NEEDS_REVIEW from exact validated artifacts."""
    if type(pipeline_generation) is not int or pipeline_generation < 0:
        raise ValueError("pipeline_generation must be a non-negative integer")
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise ValueError("decided_at must be timezone-aware")

    diagnostics = tuple(
        sorted(
            critical_source_diagnostics,
            key=lambda item: (item.source_url, item.failure_code, item.source_id or ""),
        )
    )

    input_payload = {
        "production_run_id": str(production_run_id),
        "pipeline_generation": pipeline_generation,
        "policy_version": ROUTING_POLICY_VERSION,
        "extraction_artifact": _artifact_identity(extraction_input),
        "relevance_artifact": _artifact_identity(relevance_input),
        "critical_source_diagnostics": _diagnostic_json(diagnostics),
    }
    input_hash = hashlib.sha256(_canonical_json(input_payload)).hexdigest()

    reason = _valid_artifact(
        value=extraction_input,
        run_id=production_run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        payload_type=ProductionExtractionV1,
        missing_reason=RoutingReasonCode.MISSING_EXTRACTION_ARTIFACT,
        invalid_reason=RoutingReasonCode.INVALID_EXTRACTION_ARTIFACT,
    )
    if reason is None:
        reason = _valid_artifact(
            value=relevance_input,
            run_id=production_run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
            payload_type=RelevanceProjectionV1,
            missing_reason=RoutingReasonCode.MISSING_RELEVANCE_ARTIFACT,
            invalid_reason=RoutingReasonCode.INVALID_RELEVANCE_ARTIFACT,
        )

    extraction = extraction_input.payload
    projection = relevance_input.payload
    source_availability_reason: RoutingReasonCode | None = None
    force_critical_source_review = False
    if reason is None:
        assert isinstance(extraction, ProductionExtractionV1)
        force_critical_source_review = any(
            source.tier is ProductionReferenceTier.CORE
            and source.acquisition_method
            in {
                SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION,
                SourceAcquisitionMethod.VISUAL_OCR,
                SourceAcquisitionMethod.THIRD_PARTY_EXTRACTION,
            }
            for source in extraction.sources
        )
        assert isinstance(projection, RelevanceProjectionV1)
        if extraction.subject_id != subject_id or extraction_input.artifact_hash not in (
            production_extraction_hashes(extraction)
        ):
            reason = RoutingReasonCode.PROVENANCE_MISMATCH
        elif relevance_input.artifact_hash != projection.projection_hash:
            reason = RoutingReasonCode.PROVENANCE_MISMATCH
        else:
            try:
                validate_relevance_projection_lineage(
                    projection,
                    extraction,
                    extraction_hash=extraction_input.artifact_hash or "",
                )
            except ValueError:
                reason = RoutingReasonCode.PROVENANCE_MISMATCH

    if reason is None:
        assert isinstance(extraction, ProductionExtractionV1)
        omitted_by_url = {
            omission.canonical_url: omission for omission in extraction.omitted_sources
        }
        if any(
            diagnostic.source_url not in omitted_by_url
            or diagnostic.tier != omitted_by_url[diagnostic.source_url].tier.value
            or (
                omitted_by_url[diagnostic.source_url].tier is ProductionReferenceTier.CORE
                and (
                    diagnostic.criticality is not ProductionSourceCriticality.CRITICAL
                    or diagnostic.criticality_reason
                    != ProductionSourceCriticalityReason.CORE_TIER.value
                )
            )
            or (
                omitted_by_url[diagnostic.source_url].error_code is not None
                and diagnostic.failure_code != omitted_by_url[diagnostic.source_url].error_code
            )
            for diagnostic in diagnostics
        ):
            reason = RoutingReasonCode.PROVENANCE_MISMATCH
        diagnostics_by_url = {diagnostic.source_url for diagnostic in diagnostics}
        core_omissions = tuple(
            omission
            for omission in extraction.omitted_sources
            if omission.tier is ProductionReferenceTier.CORE
        )
        diagnostics = tuple(
            sorted(
                (
                    *diagnostics,
                    *(
                        RoutingCriticalSourceDiagnosticV1(
                            source_id=None,
                            source_url=omission.canonical_url,
                            failure_code=(omission.error_code or omission.reason.value),
                            tier=omission.tier.value,
                            criticality=ProductionSourceCriticality.CRITICAL,
                            criticality_reason=ProductionSourceCriticalityReason.CORE_TIER.value,
                            policy_version=PRODUCTION_SOURCE_CRITICALITY_POLICY_VERSION,
                        )
                        for omission in core_omissions
                        if omission.canonical_url not in diagnostics_by_url
                    ),
                ),
                key=lambda item: (item.source_url, item.failure_code, item.source_id or ""),
            )
        )
        missing_diagnostic = any(
            omission.reason is ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED
            and not any(item.source_url == omission.canonical_url for item in diagnostics)
            for omission in extraction.omitted_sources
        )
        if reason is None:
            if missing_diagnostic:
                source_availability_reason = RoutingReasonCode.INCOMPLETE_EXTRACTION
            elif any(
                diagnostic.criticality is ProductionSourceCriticality.CRITICAL
                for diagnostic in diagnostics
            ):
                source_availability_reason = RoutingReasonCode.CRITICAL_SOURCE_MISSING

    status = RoutingStatus.NEEDS_REVIEW if reason is not None else RoutingStatus.DECIDED
    publication_kind: PublicationKind | None = None
    confirmed_ioc_count = contextual_ioc_count = ambiguous_ioc_count = 0

    if status is RoutingStatus.DECIDED:
        assert isinstance(extraction, ProductionExtractionV1)
        assert isinstance(projection, RelevanceProjectionV1)
        subject_iocs: set[tuple[ArtifactType, str]] = set()
        contextual_iocs: set[tuple[ArtifactType, str]] = set()
        ambiguous_iocs: set[tuple[ArtifactType, str]] = set()
        indicator_ref_by_identity = {
            (
                ref.source_document_id,
                str(payload["value"]),
                str(payload["artifact_type"]),
                str(payload["context"]),
                str(payload["evidence_quote"]),
            ): ref
            for ref, payload in extraction_evidence_elements(extraction)
            if ref.kind is EvidenceKind.INDICATOR
        }
        for source in extraction.sources:
            for item in source.indicators:
                key: tuple[ArtifactType, str] | None = None
                try:
                    normalized = normalize_indicator_value(item.value, item.artifact_type)
                    key = (item.artifact_type, normalized)
                except (TypeError, ValueError):
                    pass
                if item.indicator_status in {
                    ExtractionIndicatorStatus.CONTEXTUAL,
                    ExtractionIndicatorStatus.UNVERIFIED_CONTEXTUAL,
                }:
                    if key is not None and item.artifact_type in {
                        ArtifactType.IP,
                        ArtifactType.DOMAIN,
                        ArtifactType.URL,
                        ArtifactType.EMAIL,
                        ArtifactType.HASH,
                    }:
                        contextual_iocs.add(key)
                    continue
                evidence_ref = indicator_ref_by_identity.get(
                    (
                        source.source_document_id,
                        item.value,
                        item.artifact_type.value,
                        item.context,
                        item.evidence_quote,
                    )
                )
                if evidence_ref is None:
                    reason = RoutingReasonCode.PROVENANCE_MISMATCH
                    status = RoutingStatus.NEEDS_REVIEW
                    break
                decision = projection.classification_for(evidence_ref)
                eligible = is_subject_ioc_eligible(
                    indicator_status=item.indicator_status,
                    artifact_type=item.artifact_type,
                    value=item.value,
                    relevance=decision,
                    legitimate_services=legitimate_services,
                )
                if eligible and key is not None:
                    subject_iocs.add(key)
                elif (
                    key is not None
                    and item.artifact_type
                    in {
                        ArtifactType.IP,
                        ArtifactType.DOMAIN,
                        ArtifactType.URL,
                        ArtifactType.EMAIL,
                        ArtifactType.HASH,
                    }
                    and decision.classification is not RelevanceClassification.OUT_OF_SCOPE
                ):
                    ambiguous_iocs.add(key)
        confirmed_ioc_count = len(subject_iocs)
        contextual_ioc_count = len(contextual_iocs)
        ambiguous_ioc_count = len(ambiguous_iocs)
        if status is RoutingStatus.DECIDED:
            if force_critical_source_review:
                status = RoutingStatus.NEEDS_REVIEW
                reason = RoutingReasonCode.CRITICAL_SOURCE_MISSING
            elif subject_iocs:
                publication_kind = PublicationKind.ARTICLE
                reason = RoutingReasonCode.SUBJECT_IOC_CONFIRMED
            elif source_availability_reason is not None:
                status = RoutingStatus.NEEDS_REVIEW
                reason = source_availability_reason
            else:
                publication_kind = PublicationKind.BRIEF
                reason = RoutingReasonCode.NO_SUBJECT_IOC

    assert reason is not None
    decision_payload = _decision_payload(
        production_run_id=production_run_id,
        pipeline_generation=pipeline_generation,
        extraction_input=extraction_input,
        relevance_input=relevance_input,
        input_hash=input_hash,
        status=status,
        publication_kind=publication_kind,
        reason_code=reason,
        confirmed_ioc_count=confirmed_ioc_count,
        contextual_ioc_count=contextual_ioc_count,
        ambiguous_ioc_count=ambiguous_ioc_count,
        diagnostics=diagnostics,
    )
    decision_hash = hashlib.sha256(_canonical_json(decision_payload)).hexdigest()
    decision_id = uuid5(
        _DECISION_NAMESPACE,
        f"{production_run_id}:{pipeline_generation}:{input_hash}",
    )
    return ProductionRoutingDecisionV1(
        decision_id=decision_id,
        production_run_id=production_run_id,
        pipeline_generation=pipeline_generation,
        extraction_artifact_id=(
            extraction_input.artifact.id if extraction_input.artifact is not None else None
        ),
        extraction_artifact_hash=extraction_input.artifact_hash,
        relevance_artifact_id=(
            relevance_input.artifact.id if relevance_input.artifact is not None else None
        ),
        relevance_artifact_hash=relevance_input.artifact_hash,
        input_hash=input_hash,
        policy_version=ROUTING_POLICY_VERSION,
        status=status,
        publication_kind=publication_kind,
        reason_code=reason,
        confirmed_ioc_count=confirmed_ioc_count,
        contextual_ioc_count=contextual_ioc_count,
        ambiguous_ioc_count=ambiguous_ioc_count,
        critical_source_diagnostics=diagnostics,
        decided_at=decided_at.astimezone(UTC),
        decision_hash=decision_hash,
    )

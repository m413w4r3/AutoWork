"""Approved analyst-opinion augmentation of an immutable V5 publication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from cti_app.application.investigation_graph_source import InvestigationGraphSource
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_detection import build_publication_detection
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    CitationViolation,
    OpinionBlockType,
    opinion_bridge_drafting_summary,
    opinion_internal_text_markers,
    verify_opinion_citations,
)
from cti_app.domain.investigation import EvidenceObservation, InvestigationFinding
from cti_app.domain.investigation_graph import InvestigationGraph
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationAnalystOpinionBlockV1,
    PublicationAnalystOpinionSectionV1,
    PublicationDetectionV1,
    PublicationDocumentV5,
    PublicationDocumentV6,
    PublicationDocumentV6ErrorCode,
    PublicationDocumentV6ProvenanceV1,
    PublicationDocumentV6ValidationError,
    PublicationDocumentV7,
    publication_document_from_json,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind


class PublicationAugmentationErrorCode(StrEnum):
    BASE_DOCUMENT_REQUIRED = "publication_augmentation_base_document_required"
    BASE_IDENTITY_MISMATCH = "publication_augmentation_base_identity_mismatch"
    INVESTIGATION_IDENTITY_MISMATCH = "publication_augmentation_investigation_identity_mismatch"
    OPINION_NOT_APPROVED = "publication_augmentation_opinion_not_approved"
    BRIDGE_DRAFTING_ACK_REQUIRED = "publication_augmentation_bridge_drafting_ack_required"
    OPINION_TEXT_MARKERS_FOUND = "publication_augmentation_opinion_text_markers_found"
    OPINION_EMPTY = "publication_augmentation_opinion_empty"
    CITATIONS_INVALID = "publication_augmentation_citations_invalid"
    TLP_VIOLATION = "publication_augmentation_tlp_violation"
    DO_NOT_SUBMIT = "publication_augmentation_do_not_submit"
    BASE_ARTIFACT_NOT_CURRENT = "publication_augmentation_base_artifact_not_current"
    BASE_ARTIFACT_INVALID = "publication_augmentation_base_artifact_invalid"
    INVESTIGATION_NOT_FOUND = "publication_augmentation_investigation_not_found"
    SYNTHESIS_IDENTITY_MISMATCH = "publication_augmentation_synthesis_identity_mismatch"
    IDEMPOTENCY_CONFLICT = "publication_augmentation_idempotency_conflict"
    PUBLICATION_KIND_INVALID = "publication_augmentation_publication_kind_invalid"


class PublicationAugmentationError(ValueError):
    """Typed refusal for an opinion or base that cannot be published."""

    def __init__(
        self,
        code: PublicationAugmentationErrorCode,
        *,
        citation_violations: tuple[CitationViolation, ...] = (),
        publication_text_markers: tuple[dict[str, str], ...] = (),
    ) -> None:
        self.code = code
        self.citation_violations = citation_violations
        self.publication_text_markers = publication_text_markers
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class OpinionCitationContext:
    """Exact base and investigation identities plus code-verifiable evidence."""

    base_publication_artifact_id: UUID
    base_publication_sha256: str
    investigation_id: UUID
    approver_id: str
    findings: tuple[InvestigationFinding, ...]
    observations: tuple[EvidenceObservation, ...]
    tlp_violation: bool
    do_not_submit: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "observations", tuple(self.observations))
        if not self.approver_id.strip():
            raise ValueError("publication augmentation approver identity is required")
        if type(self.tlp_violation) is not bool or type(self.do_not_submit) is not bool:
            raise TypeError("publication augmentation policy flags must be booleans")


_BLOCK_HEADINGS = {
    OpinionBlockType.KEY_FINDINGS: "Apports de l'enquête",
    OpinionBlockType.ADDITIONAL_SAMPLES: "Échantillons complémentaires",
    OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES: "Chaîne d'infection et capacités",
    OpinionBlockType.NETWORK_AND_INFRASTRUCTURE: "Activité réseau et infrastructure",
    OpinionBlockType.CLAIM_VERIFICATION: "Vérification des affirmations",
    OpinionBlockType.PROPOSED_DETECTIONS: "Détections proposées",
    OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS: "Incertitudes et prochaines pistes",
}
_BLOCK_ORDER = {kind: index for index, kind in enumerate(OpinionBlockType)}


def augment_publication_with_opinion(
    base_publication_document: PublicationDocumentV5 | PublicationDocumentV7,
    opinion: AnalystOpinionV1,
    citation_context: OpinionCitationContext,
    *,
    infrastructure_graph: InvestigationGraph | None = None,
    detection: PublicationDetectionV1 | None = None,
) -> PublicationDocumentV6 | PublicationDocumentV7:
    """Return a V6 wrapper, preserving explicit V7 routing when present."""
    routed_base = (
        base_publication_document
        if isinstance(base_publication_document, PublicationDocumentV7)
        else None
    )
    if routed_base is not None:
        if routed_base.publication_kind is not PublicationKind.ARTICLE:
            raise PublicationAugmentationError(
                PublicationAugmentationErrorCode.PUBLICATION_KIND_INVALID
            )
        base_publication_document = routed_base.base_publication.base_publication
    if not isinstance(base_publication_document, PublicationDocumentV5):
        raise PublicationAugmentationError(PublicationAugmentationErrorCode.BASE_DOCUMENT_REQUIRED)
    if opinion.status is not AnalystOpinionStatus.APPROVED:
        raise PublicationAugmentationError(PublicationAugmentationErrorCode.OPINION_NOT_APPROVED)
    if opinion.investigation_id != citation_context.investigation_id:
        raise PublicationAugmentationError(
            PublicationAugmentationErrorCode.INVESTIGATION_IDENTITY_MISMATCH
        )
    if citation_context.tlp_violation:
        raise PublicationAugmentationError(PublicationAugmentationErrorCode.TLP_VIOLATION)
    if citation_context.do_not_submit:
        raise PublicationAugmentationError(PublicationAugmentationErrorCode.DO_NOT_SUBMIT)

    base_sha256 = publication_document_v5_sha256(base_publication_document)
    if (
        citation_context.base_publication_sha256 != base_sha256
        or opinion.base_publication_artifact_id != citation_context.base_publication_artifact_id
        or opinion.base_publication_sha256 != base_sha256
    ):
        raise PublicationAugmentationError(PublicationAugmentationErrorCode.BASE_IDENTITY_MISMATCH)
    if hashlib.sha256(opinion.canonical_bytes()).hexdigest() != opinion.content_sha256:
        raise PublicationAugmentationError(PublicationAugmentationErrorCode.CITATIONS_INVALID)
    if not opinion.renderable_blocks or any(not block.is_renderable for block in opinion.blocks):
        raise PublicationAugmentationError(PublicationAugmentationErrorCode.OPINION_EMPTY)

    citation_violations = verify_opinion_citations(
        opinion,
        citation_context.findings,
        citation_context.observations,
    )
    findings_by_id = {finding.id: finding for finding in citation_context.findings}
    observations_by_id = {
        observation.id: observation for observation in citation_context.observations
    }
    cross_investigation_citation = any(
        (finding := findings_by_id.get(reference_id)) is not None
        and finding.investigation_id != opinion.investigation_id
        for block in opinion.blocks
        for reference_id in block.finding_refs
    ) or any(
        (observation := observations_by_id.get(reference_id)) is not None
        and observation.investigation_id != opinion.investigation_id
        for block in opinion.blocks
        for reference_id in block.evidence_refs
    )
    if citation_violations or cross_investigation_citation:
        raise PublicationAugmentationError(
            PublicationAugmentationErrorCode.CITATIONS_INVALID,
            citation_violations=citation_violations,
        )

    ordered_blocks = sorted(
        opinion.renderable_blocks,
        key=lambda block: _BLOCK_ORDER[block.block_type],
    )
    section = PublicationAnalystOpinionSectionV1(
        opinion_id=opinion.id,
        opinion_version=opinion.version,
        content_sha256=opinion.content_sha256,
        blocks=tuple(
            PublicationAnalystOpinionBlockV1(
                block_type=block.block_type,
                heading=_BLOCK_HEADINGS[block.block_type],
                text=block.text,
                qualification=block.qualification,
                certainty=block.certainty,
                finding_refs=block.finding_refs,
                evidence_refs=block.evidence_refs,
            )
            for block in ordered_blocks
        ),
        unresolved_points=opinion.unresolved_points,
        protocol_figures=opinion.protocol_figures,
    )
    provenance = PublicationDocumentV6ProvenanceV1(
        base_publication_artifact_id=citation_context.base_publication_artifact_id,
        base_publication_sha256=base_sha256,
        investigation_id=opinion.investigation_id,
        opinion_id=opinion.id,
        opinion_version=opinion.version,
        opinion_content_sha256=opinion.content_sha256,
        reviewer_id=opinion.reviewer_id,
        approver_id=citation_context.approver_id,
    )
    try:
        augmented = PublicationDocumentV6(
            base_publication=base_publication_document,
            provenance=provenance,
            analyst_opinion=opinion,
            rendered_opinion=section,
            infrastructure_graph=infrastructure_graph,
            detection=detection,
        )
        if routed_base is None:
            return augmented
        return PublicationDocumentV7(
            base_publication=augmented,
            publication_kind=routed_base.publication_kind,
            routing_decision_hash=routed_base.routing_decision_hash,
            routing_policy_version=routed_base.routing_policy_version,
        )
    except PublicationDocumentV6ValidationError as exc:
        code = {
            PublicationDocumentV6ErrorCode.BASE_IDENTITY_MISMATCH: (
                PublicationAugmentationErrorCode.BASE_IDENTITY_MISMATCH
            ),
            PublicationDocumentV6ErrorCode.OPINION_EMPTY: (
                PublicationAugmentationErrorCode.OPINION_EMPTY
            ),
            PublicationDocumentV6ErrorCode.OPINION_NOT_APPROVED: (
                PublicationAugmentationErrorCode.OPINION_NOT_APPROVED
            ),
        }.get(exc.code, PublicationAugmentationErrorCode.CITATIONS_INVALID)
        raise PublicationAugmentationError(code) from exc


class PublicationPreviewRenderer(Protocol):
    async def render_preview(self, publication_artifact_id: UUID) -> object: ...


class PublicationAugmentationArtifactService:
    """Persist one V6 PUBLICATION version without executing the production workflow."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        preview_renderer: PublicationPreviewRenderer | None = None,
        graph_source: InvestigationGraphSource | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._preview_renderer = preview_renderer
        self._graph_source = graph_source or InvestigationGraphSource(
            artifact_reader=artifact_store
        )

    async def publish(
        self,
        *,
        base_publication_artifact_id: UUID,
        opinion: AnalystOpinionV1,
        citation_context: OpinionCitationContext,
        bridge_drafting_gap_acknowledged: bool = False,
    ) -> ProductionArtifact:
        markers = opinion_internal_text_markers(opinion)
        if markers:
            raise PublicationAugmentationError(
                PublicationAugmentationErrorCode.OPINION_TEXT_MARKERS_FOUND,
                publication_text_markers=markers,
            )
        drafting = opinion_bridge_drafting_summary(opinion)
        gap_acknowledged = bool(drafting["requires_acknowledgement"]) and (
            opinion.bridge_drafting_gap_acknowledged or bridge_drafting_gap_acknowledged
        )
        if drafting["requires_acknowledgement"] and not gap_acknowledged:
            raise PublicationAugmentationError(
                PublicationAugmentationErrorCode.BRIDGE_DRAFTING_ACK_REQUIRED
            )
        if citation_context.base_publication_artifact_id != base_publication_artifact_id:
            raise PublicationAugmentationError(
                PublicationAugmentationErrorCode.BASE_IDENTITY_MISMATCH
            )
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(opinion.investigation_id)
            if investigation is None:
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.INVESTIGATION_NOT_FOUND
                )
            opinion_run_id = investigation.production_run_id
            current = await uow.production_artifacts.get_current_for_revision(
                opinion_run_id, ProductionArtifactStage.PUBLICATION.value
            )
            prior_artifacts = await uow.production_artifacts.list_for_run(opinion_run_id)
            existing = next(
                (
                    item
                    for item in prior_artifacts
                    if item.stage is ProductionArtifactStage.PUBLICATION
                    and item.metadata.get("previous_publication_artifact_id")
                    == str(base_publication_artifact_id)
                    and isinstance(item.metadata.get("publication_augmentation"), dict)
                    and item.metadata["publication_augmentation"].get("opinion_id")
                    == str(opinion.id)
                    and item.metadata["publication_augmentation"].get("opinion_version")
                    == opinion.version
                    and item.metadata["publication_augmentation"].get("opinion_content_sha256")
                    == opinion.content_sha256
                ),
                None,
            )
            if existing is not None:
                augmentation_metadata = existing.metadata["publication_augmentation"]
                if augmentation_metadata.get("approver_id") != citation_context.approver_id or (
                    "bridge_drafting_gap_acknowledged" in augmentation_metadata
                    and bool(augmentation_metadata["bridge_drafting_gap_acknowledged"])
                    != gap_acknowledged
                ):
                    raise PublicationAugmentationError(
                        PublicationAugmentationErrorCode.IDEMPOTENCY_CONFLICT
                    )
                return existing
            if current is None or current.id != base_publication_artifact_id:
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.BASE_ARTIFACT_NOT_CURRENT
                )
            base_artifact = await uow.production_artifacts.get(base_publication_artifact_id)
            if (
                base_artifact is None
                or base_artifact.stage is not ProductionArtifactStage.PUBLICATION
                or base_artifact.status is not ProductionArtifactStatus.VERIFIED
                or base_artifact.canonical_blob_id is None
            ):
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.BASE_ARTIFACT_INVALID
                )
            if (
                investigation.production_run_id != base_artifact.production_run_id
                or investigation.subject_id != base_artifact.subject_id
                or investigation.synthesis_artifact_id != opinion.synthesis_artifact_id
            ):
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.INVESTIGATION_IDENTITY_MISMATCH
                )
            synthesis_artifact = await uow.production_artifacts.get(opinion.synthesis_artifact_id)
            if (
                synthesis_artifact is None
                or synthesis_artifact.stage is not ProductionArtifactStage.SYNTHESIS
                or synthesis_artifact.status is not ProductionArtifactStatus.VERIFIED
                or synthesis_artifact.production_run_id != base_artifact.production_run_id
                or synthesis_artifact.canonical_blob_id is None
            ):
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.SYNTHESIS_IDENTITY_MISMATCH
                )
            base_bytes = await self._artifact_store.read_bytes(base_artifact.canonical_blob_id)
            base_blob_sha256 = hashlib.sha256(base_bytes).hexdigest()
            if base_blob_sha256 != citation_context.base_publication_sha256:
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.BASE_IDENTITY_MISMATCH
                )
            synthesis_bytes = await self._artifact_store.read_bytes(
                synthesis_artifact.canonical_blob_id
            )
            if hashlib.sha256(synthesis_bytes).hexdigest() != opinion.synthesis_sha256:
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.SYNTHESIS_IDENTITY_MISMATCH
                )
            try:
                base_payload = json.loads(base_bytes)
                base_document = publication_document_from_json(base_payload)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.BASE_ARTIFACT_INVALID
                ) from exc
            if not isinstance(base_document, PublicationDocumentV5):
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.BASE_ARTIFACT_INVALID
                )
            if publication_document_v5_sha256(base_document) != base_blob_sha256:
                raise PublicationAugmentationError(
                    PublicationAugmentationErrorCode.BASE_IDENTITY_MISMATCH
                )

            infrastructure_graph = await self._graph_source.project(
                investigation_id=opinion.investigation_id,
                publication=base_document,
                observations=citation_context.observations,
                findings=citation_context.findings,
            )
            detection = await build_publication_detection(
                uow,
                self._artifact_store,
                investigation_id=opinion.investigation_id,
                subject_id=investigation.subject_id,
                opinion=opinion,
            )
            augmented = augment_publication_with_opinion(
                base_document,
                opinion,
                citation_context,
                infrastructure_graph=infrastructure_graph,
                detection=detection,
            )
            payload = serialize_publication_document(augmented)
            canonical_bytes = ProductionArtifactStore.canonical_json_bytes(payload)
            input_hash = hashlib.sha256(canonical_bytes).hexdigest()
            next_version = (
                max(
                    (
                        item.version
                        for item in prior_artifacts
                        if item.stage is ProductionArtifactStage.PUBLICATION
                    ),
                    default=0,
                )
                + 1
            )
            _, canonical_blob_id, _ = await self._artifact_store.store_stage_payloads(
                canonical=payload
            )
            if canonical_blob_id is None:
                raise RuntimeError("Augmented PUBLICATION body was not persisted")
            artifact = ProductionArtifact(
                production_run_id=base_artifact.production_run_id,
                subject_id=base_artifact.subject_id,
                stage=ProductionArtifactStage.PUBLICATION,
                version=next_version,
                input_hash=input_hash,
                canonical_blob_id=canonical_blob_id,
                metadata={
                    "previous_publication_artifact_id": str(base_artifact.id),
                    "publication_augmentation": {
                        "investigation_id": str(opinion.investigation_id),
                        "opinion_id": str(opinion.id),
                        "opinion_version": opinion.version,
                        "opinion_content_sha256": opinion.content_sha256,
                        "reviewer_id": opinion.reviewer_id,
                        "approver_id": citation_context.approver_id,
                        "bridge_drafting_gap_acknowledged": gap_acknowledged,
                    },
                },
            )
            await uow.production_artifacts.append(artifact)
            await uow.commit()

        if self._preview_renderer is not None:
            await self._preview_renderer.render_preview(artifact.id)
        return artifact


__all__ = [
    "OpinionCitationContext",
    "PublicationAugmentationArtifactService",
    "PublicationAugmentationError",
    "PublicationAugmentationErrorCode",
    "augment_publication_with_opinion",
]

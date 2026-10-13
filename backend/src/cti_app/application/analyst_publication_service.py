"""Creation of one linked, augmented PUBLICATION for an approved opinion."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from cti_app.application.analyst_opinion_service import (
    OpinionError,
    OpinionErrorCode,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import (
    OpinionCitationContext,
    PublicationAugmentationArtifactService,
    PublicationAugmentationError,
    PublicationAugmentationErrorCode,
)
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    verify_opinion_citations,
)
from cti_app.domain.investigation import TLPLevel
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_from_json,
    publication_document_v5_sha256,
)


class AnalystPublicationService:
    """Publish an approved opinion as a new, idempotent PUBLICATION artifact."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        augmentation_service: PublicationAugmentationArtifactService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._augmentation_service = augmentation_service or PublicationAugmentationArtifactService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
        )

    async def publish_augmented_article(
        self,
        opinion_id: UUID,
        version: int,
        actor: str,
        expected_base_publication_sha256: str,
    ) -> ProductionArtifact:
        if not actor.strip():
            raise OpinionError(OpinionErrorCode.INVALID_STATE)
        async with self._uow_factory() as uow:
            opinion = await uow.analyst_opinions.get(opinion_id, version=version)
            if opinion is None:
                raise OpinionError(OpinionErrorCode.OPINION_NOT_FOUND)
            if opinion.status is not AnalystOpinionStatus.APPROVED:
                raise OpinionError(OpinionErrorCode.OPINION_NOT_APPROVED)
            if opinion.base_publication_sha256 != expected_base_publication_sha256:
                raise OpinionError(OpinionErrorCode.STALE_BASE)
            investigation = await uow.analyst_investigations.get(opinion.investigation_id)
            if investigation is None:
                raise OpinionError(OpinionErrorCode.INVESTIGATION_NOT_FOUND)
            base = await uow.production_artifacts.get(opinion.base_publication_artifact_id)
            if base is not None and base.status is ProductionArtifactStatus.STALE:
                raise OpinionError(OpinionErrorCode.STALE_BASE)
            if (
                base is None
                or base.stage is not ProductionArtifactStage.PUBLICATION
                or base.status is not ProductionArtifactStatus.VERIFIED
                or base.canonical_blob_id is None
                or base.production_run_id != investigation.production_run_id
                or base.subject_id != investigation.subject_id
            ):
                raise OpinionError(OpinionErrorCode.BASE_INVALID)
            base_bytes = await self._artifact_store.read_bytes(base.canonical_blob_id)
            base_sha256 = hashlib.sha256(base_bytes).hexdigest()
            try:
                document = publication_document_from_json(json.loads(base_bytes))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise OpinionError(OpinionErrorCode.BASE_INVALID) from exc
            if (
                not isinstance(document, PublicationDocumentV5)
                or publication_document_v5_sha256(document) != base_sha256
            ):
                raise OpinionError(OpinionErrorCode.BASE_INVALID)
            if base_sha256 != expected_base_publication_sha256:
                raise OpinionError(OpinionErrorCode.STALE_BASE)

            findings = tuple(
                await uow.investigation_findings.list_for_investigation(opinion.investigation_id)
            )
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(opinion.investigation_id)
            )
            violations = verify_opinion_citations(opinion, findings, observations)
            finding_by_id = {item.id: item for item in findings}
            observation_by_id = {item.id: item for item in observations}
            cross_investigation = any(
                (finding := finding_by_id.get(ref)) is not None
                and finding.investigation_id != opinion.investigation_id
                for block in opinion.blocks
                for ref in block.finding_refs
            ) or any(
                (observation := observation_by_id.get(ref)) is not None
                and observation.investigation_id != opinion.investigation_id
                for block in opinion.blocks
                for ref in block.evidence_refs
            )
            if violations or cross_investigation:
                raise OpinionError(
                    OpinionErrorCode.CITATIONS_INVALID,
                    citation_violations=violations,
                )

            envelope = await uow.investigation_envelopes.get(opinion.investigation_id)
            if envelope is None or envelope.disclosure.tlp is TLPLevel.RED:
                raise OpinionError(OpinionErrorCode.TLP_FORBIDDEN)
            if envelope.disclosure.do_not_submit:
                raise OpinionError(OpinionErrorCode.DO_NOT_SUBMIT)
            context = OpinionCitationContext(
                base_publication_artifact_id=base.id,
                base_publication_sha256=base_sha256,
                investigation_id=opinion.investigation_id,
                approver_id=actor.strip(),
                findings=findings,
                observations=observations,
                tlp_violation=False,
                do_not_submit=False,
            )

        try:
            return await self._augmentation_service.publish(
                base_publication_artifact_id=base.id,
                opinion=opinion,
                citation_context=context,
            )
        except PublicationAugmentationError as exc:
            if exc.code is PublicationAugmentationErrorCode.BASE_ARTIFACT_NOT_CURRENT:
                raise OpinionError(OpinionErrorCode.STALE_BASE) from exc
            if exc.code is PublicationAugmentationErrorCode.DO_NOT_SUBMIT:
                raise OpinionError(OpinionErrorCode.DO_NOT_SUBMIT) from exc
            if exc.code is PublicationAugmentationErrorCode.TLP_VIOLATION:
                raise OpinionError(OpinionErrorCode.TLP_FORBIDDEN) from exc
            if exc.code is PublicationAugmentationErrorCode.OPINION_NOT_APPROVED:
                raise OpinionError(OpinionErrorCode.OPINION_NOT_APPROVED) from exc
            if exc.code is PublicationAugmentationErrorCode.CITATIONS_INVALID:
                raise OpinionError(
                    OpinionErrorCode.CITATIONS_INVALID,
                    citation_violations=exc.citation_violations,
                ) from exc
            raise OpinionError(OpinionErrorCode.BASE_IDENTITY_MISMATCH) from exc


__all__ = ["AnalystPublicationService"]

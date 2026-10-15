"""Ephemeral PDF previews for exact, unapproved analyst-opinion versions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cti_app.application.analyst_opinion_service import OpinionError, OpinionErrorCode
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import CompiledTypstDocument
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    verify_opinion_citations,
)
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.production_synthesis import (
    ProductionSynthesisV1,
    production_synthesis_from_json,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_from_json,
    publication_document_v5_sha256,
)


class DraftOpinionPdfRenderer(Protocol):
    async def render_draft_preview(
        self,
        document: PublicationDocumentV5,
        opinion: AnalystOpinionV1,
        synthesis: ProductionSynthesisV1,
    ) -> CompiledTypstDocument: ...


@dataclass(frozen=True, slots=True)
class AnalystOpinionDraftPdf:
    content: bytes
    sha256: str
    opinion_id: UUID
    opinion_version: int
    opinion_sha256: str
    base_publication_sha256: str


class AnalystOpinionPreviewService:
    """Verify the current V5/opinion pins, then render to a private temp workspace."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        renderer: DraftOpinionPdfRenderer,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._renderer = renderer

    async def preview_pdf(
        self,
        opinion_id: UUID,
        *,
        version: int,
        expected_opinion_sha256: str,
        expected_base_publication_sha256: str,
    ) -> AnalystOpinionDraftPdf:
        async with self._uow_factory() as uow:
            opinion = await uow.analyst_opinions.get(opinion_id, version=version)
            current = await uow.analyst_opinions.get(opinion_id)
            if opinion is None or current is None:
                raise OpinionError(OpinionErrorCode.OPINION_NOT_FOUND)
            if (
                current.version != version
                or current.content_sha256 != expected_opinion_sha256
                or opinion.content_sha256 != expected_opinion_sha256
            ):
                raise OpinionError(OpinionErrorCode.STALE_VERSION)
            if opinion.status is not AnalystOpinionStatus.DRAFT:
                raise OpinionError(OpinionErrorCode.OPINION_NOT_DRAFT)

            investigation = await uow.analyst_investigations.get(opinion.investigation_id)
            if investigation is None:
                raise OpinionError(OpinionErrorCode.INVESTIGATION_NOT_FOUND)
            current_publication = await uow.production_artifacts.get_current_for_revision(
                investigation.production_run_id,
                ProductionArtifactStage.PUBLICATION.value,
            )
            base = await uow.production_artifacts.get(opinion.base_publication_artifact_id)
            synthesis_artifact = await uow.production_artifacts.get(opinion.synthesis_artifact_id)
            if (
                base is None
                or current_publication is None
                or current_publication.id != base.id
                or base.stage is not ProductionArtifactStage.PUBLICATION
                or base.status is not ProductionArtifactStatus.VERIFIED
                or base.canonical_blob_id is None
                or base.production_run_id != investigation.production_run_id
                or base.subject_id != investigation.subject_id
                or synthesis_artifact is None
                or synthesis_artifact.stage is not ProductionArtifactStage.SYNTHESIS
                or synthesis_artifact.status is not ProductionArtifactStatus.VERIFIED
                or synthesis_artifact.production_run_id != investigation.production_run_id
                or synthesis_artifact.canonical_blob_id is None
                or investigation.synthesis_artifact_id != opinion.synthesis_artifact_id
            ):
                raise OpinionError(OpinionErrorCode.STALE_BASE)
            findings = tuple(
                await uow.investigation_findings.list_for_investigation(opinion.investigation_id)
            )
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(opinion.investigation_id)
            )

        try:
            base_bytes = await self._artifact_store.read_bytes(base.canonical_blob_id)
            synthesis_bytes = await self._artifact_store.read_bytes(
                synthesis_artifact.canonical_blob_id
            )
            raw_document = json.loads(base_bytes)
            document = publication_document_from_json(raw_document)
            raw_synthesis = json.loads(synthesis_bytes)
            parsed_synthesis = production_synthesis_from_json(raw_synthesis)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise OpinionError(OpinionErrorCode.BASE_INVALID) from exc
        except Exception as exc:
            raise OpinionError(OpinionErrorCode.BASE_INVALID) from exc

        base_sha256 = hashlib.sha256(base_bytes).hexdigest()
        synthesis_sha256 = hashlib.sha256(synthesis_bytes).hexdigest()
        if (
            not isinstance(document, PublicationDocumentV5)
            or publication_document_v5_sha256(document) != base_sha256
            or base_sha256 != opinion.base_publication_sha256
            or base_sha256 != expected_base_publication_sha256
            or synthesis_sha256 != opinion.synthesis_sha256
            or parsed_synthesis.subject_id != document.subject_id
            or parsed_synthesis.title != document.title
            or parsed_synthesis.publication_language != document.publication_language
        ):
            raise OpinionError(OpinionErrorCode.STALE_BASE)

        citation_violations = verify_opinion_citations(opinion, findings, observations)
        if citation_violations:
            raise OpinionError(
                OpinionErrorCode.CITATIONS_INVALID,
                citation_violations=citation_violations,
            )

        compiled = await self._renderer.render_draft_preview(document, opinion, parsed_synthesis)
        content = compiled.content
        renderer_sha256 = compiled.sha256
        pdf_sha256 = hashlib.sha256(content).hexdigest()
        if pdf_sha256 != renderer_sha256:
            raise ValueError("Draft preview PDF SHA-256 does not match its output")
        return AnalystOpinionDraftPdf(
            content=content,
            sha256=pdf_sha256,
            opinion_id=opinion.id,
            opinion_version=opinion.version,
            opinion_sha256=opinion.content_sha256,
            base_publication_sha256=base_sha256,
        )


__all__ = ["AnalystOpinionDraftPdf", "AnalystOpinionPreviewService"]

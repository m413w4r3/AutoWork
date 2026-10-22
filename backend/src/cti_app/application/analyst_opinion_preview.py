"""Ephemeral PDF previews for exact, unapproved analyst-opinion versions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.analyst_opinion_service import OpinionError, OpinionErrorCode
from cti_app.application.investigation_graph_source import InvestigationGraphSource
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_rendering import DraftPublicationRender
from cti_app.application.typst_compilation import CompiledTypstDocument
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionProtocolFigureV1,
    verify_opinion_citations,
)
from cti_app.domain.investigation import EvidenceObservation, InvestigationFinding
from cti_app.domain.investigation_graph import InvestigationGraph
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
        *,
        infrastructure_graph: InvestigationGraph | None = None,
        findings: tuple[InvestigationFinding, ...] = (),
        evidence_observations: tuple[EvidenceObservation, ...] = (),
        base_publication_identity: dict[str, Any],
    ) -> DraftPublicationRender | CompiledTypstDocument: ...


@dataclass(frozen=True, slots=True)
class AnalystOpinionDraftPdf:
    content: bytes
    sha256: str
    opinion_id: UUID
    opinion_version: int
    opinion_sha256: str
    base_publication_sha256: str
    lineage_manifest: dict[str, Any] = field(default_factory=dict)


class AnalystOpinionPreviewService:
    """Verify the current V5/opinion pins, then render to a private temp workspace."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        renderer: DraftOpinionPdfRenderer,
        graph_source: InvestigationGraphSource | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._renderer = renderer
        self._graph_source = graph_source or InvestigationGraphSource(
            artifact_reader=artifact_store
        )

    async def preview_pdf(
        self,
        opinion_id: UUID,
        *,
        version: int,
        expected_opinion_sha256: str,
        expected_base_publication_sha256: str,
        protocol_figures: tuple[OpinionProtocolFigureV1, ...] | None = None,
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
            current_run = None
            latest_investigation = None
            run_repository = getattr(uow, "production_runs", None)
            current_run_getter = getattr(run_repository, "get_current_for_subject", None)
            if callable(current_run_getter):
                current_run = await current_run_getter(investigation.subject_id)
            investigation_getter = getattr(uow.analyst_investigations, "get_for_run", None)
            if callable(investigation_getter):
                latest_investigation = await investigation_getter(investigation.production_run_id)
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

        render_opinion = opinion
        if protocol_figures is not None:
            render_opinion = replace(
                opinion,
                protocol_figures=protocol_figures,
                content_sha256="",
            )

        infrastructure_graph = await self._graph_source.project(
            investigation_id=opinion.investigation_id,
            publication=document,
            observations=observations,
            findings=findings,
        )
        if infrastructure_graph is None:
            compiled = await self._renderer.render_draft_preview(
                document,
                render_opinion,
                parsed_synthesis,
                findings=findings,
                evidence_observations=observations,
                base_publication_identity={
                    "artifact": {
                        "id": str(base.id),
                        "version": base.version,
                        "sha256": base_sha256,
                        "document_schema": "PublicationDocumentV5",
                    },
                    "investigation_id": str(investigation.id),
                    "production_run_id": str(investigation.production_run_id),
                },
            )
        else:
            compiled = await self._renderer.render_draft_preview(
                document,
                render_opinion,
                parsed_synthesis,
                infrastructure_graph=infrastructure_graph,
                findings=findings,
                evidence_observations=observations,
                base_publication_identity={
                    "artifact": {
                        "id": str(base.id),
                        "version": base.version,
                        "sha256": base_sha256,
                        "document_schema": "PublicationDocumentV5",
                    },
                    "investigation_id": str(investigation.id),
                    "production_run_id": str(investigation.production_run_id),
                },
            )
        rendered = (
            compiled.compiled_document if isinstance(compiled, DraftPublicationRender) else compiled
        )
        content = rendered.content
        renderer_sha256 = rendered.sha256
        pdf_sha256 = hashlib.sha256(content).hexdigest()
        if pdf_sha256 != renderer_sha256:
            raise ValueError("Draft preview PDF SHA-256 does not match its output")
        history_reasons: list[str] = []
        if current_run is not None and current_run.id != investigation.production_run_id:
            history_reasons.append("investigation_run_is_not_current")
        if latest_investigation is not None and latest_investigation.id != investigation.id:
            history_reasons.append("investigation_is_not_current_for_run")
        lineage: dict[str, Any] = (
            dict(compiled.lineage)
            if isinstance(compiled, DraftPublicationRender)
            else {
                "schema_version": "pdf-lineage.v1",
                "nature": "opinion_draft_preview",
                "artifact": {
                    "id": str(base.id),
                    "version": base.version,
                    "sha256": base_sha256,
                    "document_schema": "PublicationDocumentV5",
                },
                "investigation_id": str(investigation.id),
                "production_run_id": str(investigation.production_run_id),
                "renderer": {
                    "name": rendered.compiler,
                    "version": rendered.compiler_version,
                    "identity_sha256": None,
                },
                "template": None,
                "compiler": {
                    "name": rendered.compiler,
                    "version": rendered.compiler_version,
                    "identity_sha256": None,
                    "binary_sha256": None,
                },
                "rendered_at": datetime.now(UTC).isoformat(),
            }
        )
        lineage.update(
            {
                "render_id": None,
                "opinion": {
                    "id": str(opinion.id),
                    "version": opinion.version,
                    "status": opinion.status.value,
                    "sha256": opinion.content_sha256,
                },
                "render_data_sha256": (
                    compiled.render_data_sha256
                    if isinstance(compiled, DraftPublicationRender)
                    else None
                ),
                "pdf_sha256": pdf_sha256,
                "is_current": not history_reasons,
                "historical": bool(history_reasons),
                "history_reasons": history_reasons,
            }
        )
        return AnalystOpinionDraftPdf(
            content=content,
            sha256=pdf_sha256,
            opinion_id=opinion.id,
            opinion_version=opinion.version,
            opinion_sha256=opinion.content_sha256,
            base_publication_sha256=base_sha256,
            lineage_manifest=lineage,
        )


__all__ = ["AnalystOpinionDraftPdf", "AnalystOpinionPreviewService"]

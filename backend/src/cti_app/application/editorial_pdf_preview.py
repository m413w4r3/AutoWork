"""Explicit, current-pin publication assembly and PDF rendering for the editor."""

from __future__ import annotations

import hashlib
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.editorial_revision_effects import EditorialRevisionEffects
from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_references import production_reference_corpus_from_json
from cti_app.application.production_routing import get_adopted_production_routing_decision
from cti_app.application.publication_assembly import PublicationAssemblyService
from cti_app.application.publication_builder import PublicationAssemblyValidationError
from cti_app.domain.editorial_proposal_impact import (
    EditorialPdfPreview,
    EditorialPdfPreviewStatus,
    project_editorial_pdf_preview,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRunStatus,
)
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_from_json
from cti_app.domain.production_extraction import production_extraction_from_json
from cti_app.domain.production_relevance import relevance_projection_from_json
from cti_app.domain.production_synthesis import production_synthesis_from_json
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.publication_render import PublicationRender
from cti_app.domain.typst_render import TypstRenderStatus
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler


class EditorialPdfRegenerationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require_explicit_publication_kind(kind: PublicationKind | None) -> PublicationKind:
    if not isinstance(kind, PublicationKind):
        raise EditorialPdfRegenerationError("publication_kind_unresolved")
    return kind


class PublicationPreviewRenderer(Protocol):
    async def render_preview(self, publication_artifact_id: UUID) -> Any: ...


class EditorialPdfPreviewService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        workspace_service: EditorialWorkspaceService,
        render_service: PublicationPreviewRenderer,
        media_asset_store: Any | None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._workspace_service = workspace_service
        self._render_service = render_service
        self._media_asset_store = media_asset_store
        self._revision_effects = EditorialRevisionEffects(workspace_service=workspace_service)

    async def regenerate(self, subject_id: UUID) -> EditorialPdfPreview:
        artifact, canonical_sha256 = await self._assemble_current_publication(subject_id)
        attempt = await self._render_service.render_preview(artifact.id)
        snapshot = await self._workspace_service.snapshot(subject_id)
        artifact_is_current = snapshot.get("pdf_status") == "CURRENT" and snapshot.get(
            "pdf_artifact_id"
        ) == str(artifact.id)
        render = attempt.render if isinstance(attempt.render, PublicationRender) else None
        preview = project_editorial_pdf_preview(
            subject_id=subject_id,
            current_artifact=artifact if artifact_is_current else None,
            current_canonical_sha256=canonical_sha256 if artifact_is_current else None,
            render=render,
            publication_stale=not artifact_is_current,
        )
        if attempt.status is TypstRenderStatus.FAILED and render is None and artifact_is_current:
            code = attempt.error_code or "publication_preview_generation_failed"
            return EditorialPdfPreview(
                status=EditorialPdfPreviewStatus.FAILED,
                artifact_id=artifact.id,
                artifact_version=artifact.version,
                artifact_input_hash=artifact.input_hash,
                artifact_canonical_sha256=canonical_sha256,
                render_id=None,
                render_identity=None,
                error_code=code,
                error_message=(
                    "La génération du PDF a échoué. Vérifiez la configuration Typst puis réessayez."
                ),
                pdf_url=None,
            )
        return preview

    async def _assemble_current_publication(
        self, subject_id: UUID
    ) -> tuple[ProductionArtifact, str]:
        async with self._uow_factory() as uow:
            initial_run = await uow.production_runs.get_current_for_subject(subject_id)
            if initial_run is None:
                raise EditorialPdfRegenerationError("production_run_missing")
            guard = await self._revision_effects.acquire_run_guard(uow, initial_run.id)
            run = await self._revision_effects.lock_run_row(uow, guard)
            if run.subject_id != subject_id:
                raise EditorialPdfRegenerationError("production_run_subject_mismatch")
            if run.status is not ProductionRunStatus.READY:
                raise EditorialPdfRegenerationError("production_run_not_ready")

            workspace_record = await self._workspace_service.snapshot_record_in_uow(uow, subject_id)
            publication_kind = require_explicit_publication_kind(workspace_record.publication_kind)
            routing = await get_adopted_production_routing_decision(uow=uow, run=run)
            if (
                routing is None
                or routing.publication_kind is not publication_kind
                or not routing.decision_hash
                or not routing.policy_version
            ):
                raise EditorialPdfRegenerationError("publication_routing_unavailable")

            production_input = await uow.production_input_snapshots.get_by_run(run.id)
            references_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.REFERENCES.value
            )
            extraction_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EXTRACTION.value
            )
            projection_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
            )
            synthesis_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.SYNTHESIS.value
            )
            enrichment_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
            )
            required_artifacts = (references_artifact, extraction_artifact, synthesis_artifact)
            if production_input is None or any(item is None for item in required_artifacts):
                raise EditorialPdfRegenerationError("publication_preview_inputs_unavailable")
            if publication_kind is PublicationKind.BRIEF and projection_artifact is None:
                raise EditorialPdfRegenerationError("publication_preview_inputs_unavailable")
            if publication_kind is PublicationKind.ARTICLE and enrichment_artifact is None:
                raise EditorialPdfRegenerationError("publication_preview_inputs_unavailable")
            artifacts = tuple(
                item
                for item in (*required_artifacts, projection_artifact, enrichment_artifact)
                if item is not None
            )
            for item in artifacts:
                if (
                    item.status is not ProductionArtifactStatus.VERIFIED
                    or item.canonical_blob_id is None
                ):
                    raise EditorialPdfRegenerationError("publication_preview_inputs_unavailable")

            assert references_artifact is not None
            assert extraction_artifact is not None
            assert synthesis_artifact is not None
            assert references_artifact.canonical_blob_id is not None
            assert extraction_artifact.canonical_blob_id is not None
            assert synthesis_artifact.canonical_blob_id is not None
            try:
                references = production_reference_corpus_from_json(
                    await self._artifact_store.read_json(references_artifact.canonical_blob_id)
                )
                extraction = production_extraction_from_json(
                    await self._artifact_store.read_json(extraction_artifact.canonical_blob_id)
                )
                synthesis = production_synthesis_from_json(
                    await self._artifact_store.read_json(synthesis_artifact.canonical_blob_id)
                )
                relevance_projection = (
                    relevance_projection_from_json(
                        await self._artifact_store.read_json(
                            _require_canonical_blob_id(projection_artifact)
                        )
                    )
                    if projection_artifact is not None
                    else None
                )
                editorial_enrichment = (
                    editorial_enrichment_from_json(
                        await self._artifact_store.read_json(
                            _require_canonical_blob_id(enrichment_artifact)
                        )
                    )
                    if enrichment_artifact is not None
                    else None
                )
            except (KeyError, TypeError, ValueError) as error:
                raise EditorialPdfRegenerationError("publication_preview_inputs_invalid") from error

            assembly = PublicationAssemblyService(
                self._artifact_store,
                uow.production_artifacts,
                media_asset_store=self._media_asset_store,
                diagram_compiler=D2DiagramCompiler(),
            )
            try:
                result = await assembly.assemble_publication(
                    run=run,
                    snapshot=production_input,
                    references=references,
                    extraction=extraction,
                    relevance_projection=relevance_projection,
                    synthesis=synthesis,
                    editorial_enrichment=editorial_enrichment,
                    publication_kind=publication_kind,
                    editorial_enrichment_artifact_id=(
                        enrichment_artifact.id if enrichment_artifact is not None else None
                    ),
                    editorial_enrichment_artifact_input_hash=(
                        enrichment_artifact.input_hash if enrichment_artifact is not None else None
                    ),
                    workspace_snapshot_record=workspace_record,
                    routing_decision_hash=routing.decision_hash,
                    routing_policy_version=routing.policy_version,
                    metadata_extra={
                        "input_artifacts": {
                            "references_artifact_id": str(references_artifact.id),
                            "extraction_artifact_id": str(extraction_artifact.id),
                            "relevance_projection_artifact_id": (
                                str(projection_artifact.id)
                                if projection_artifact is not None
                                else None
                            ),
                            "synthesis_artifact_id": str(synthesis_artifact.id),
                            **(
                                {"editorial_enrichment_artifact_id": str(enrichment_artifact.id)}
                                if enrichment_artifact is not None
                                else {}
                            ),
                        }
                    },
                )
            except PublicationAssemblyValidationError as error:
                raise EditorialPdfRegenerationError(error.code.value) from error
            except (TypeError, ValueError) as error:
                raise EditorialPdfRegenerationError("publication_preview_inputs_invalid") from error
            await uow.commit()

        artifact = result.artifact
        if artifact.canonical_blob_id is None:
            raise EditorialPdfRegenerationError("publication_preview_artifact_unavailable")
        canonical_bytes = await self._artifact_store.read_bytes(artifact.canonical_blob_id)
        return artifact, hashlib.sha256(canonical_bytes).hexdigest()


def _require_canonical_blob_id(artifact: ProductionArtifact) -> UUID:
    if artifact.canonical_blob_id is None:
        raise EditorialPdfRegenerationError("publication_preview_inputs_unavailable")
    return artifact.canonical_blob_id

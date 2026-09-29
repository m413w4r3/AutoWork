"""Persist canonical publication documents as production artifacts."""

from __future__ import annotations

from cti_app.application.persistence import ProductionArtifactRepository
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_builder import (
    build_publication_document_v3,
    compute_assembly_input_hash,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1


class PublicationAssemblyService:
    """Build and persist the canonical publication for one production run."""

    def __init__(
        self,
        artifact_store: ProductionArtifactStore,
        production_artifacts: ProductionArtifactRepository,
    ) -> None:
        self._artifact_store = artifact_store
        self._production_artifacts = production_artifacts

    async def assemble_publication(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        references: ProductionReferenceCorpusV1,
        extraction: ProductionExtractionV1,
        synthesis: ProductionSynthesisV1,
    ) -> ProductionArtifact:
        if (
            snapshot.production_run_id != run.id
            or snapshot.edition_id != run.edition_id
            or snapshot.subject_id != run.subject_id
        ):
            raise ValueError("Production input snapshot must belong to the production run")
        document = build_publication_document_v3(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )

        input_hash = compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )
        _, canonical_blob_id, _ = await self._artifact_store.store_stage_payloads(
            canonical=document.to_json()
        )
        if canonical_blob_id is None:
            raise RuntimeError("Canonical publication body was not persisted")

        prior_versions = [
            artifact.version
            for artifact in await self._production_artifacts.list_for_run(run.id)
            if artifact.stage is ProductionArtifactStage.PUBLICATION
        ]
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=max(prior_versions, default=0) + 1,
            input_hash=input_hash,
            canonical_blob_id=canonical_blob_id,
        )
        await self._production_artifacts.append(artifact)
        return artifact

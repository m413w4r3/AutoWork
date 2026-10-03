"""Persist canonical publication documents as production artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cti_app.application.persistence import ProductionArtifactRepository
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import validate_editorial_enrichment
from cti_app.application.publication_builder import (
    PublicationAssemblyValidationError,
    build_publication_document_v5,
    compute_assembly_input_hash,
)
from cti_app.domain.errors import BlobIntegrityError, EntityNotFoundError
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentV1
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_relevance import RelevanceProjectionV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from cti_app.domain.publication import PublicationAssemblyErrorCode
from cti_app.domain.publication_document import serialize_publication_document


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
        editorial_enrichment: EditorialEnrichmentV1,
        relevance_projection: RelevanceProjectionV1 | None = None,
        metadata_extra: Mapping[str, Any] | None = None,
    ) -> ProductionArtifact:
        if (
            snapshot.production_run_id != run.id
            or snapshot.edition_id != run.edition_id
            or snapshot.subject_id != run.subject_id
        ):
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                "Production input snapshot must belong to the production run",
            )
        try:
            validate_editorial_enrichment(
                editorial_enrichment, extraction=extraction, synthesis=synthesis
            )
        except ValueError as exc:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                str(exc),
            ) from exc
        if editorial_enrichment.production_input_hash != snapshot.input_hash:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                "Editorial enrichment does not match the production input snapshot",
            )
        document = build_publication_document_v5(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            relevance_projection=relevance_projection,
            synthesis=synthesis,
            editorial_enrichment=editorial_enrichment,
        )

        input_hash = compute_assembly_input_hash(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=editorial_enrichment,
            relevance_projection=relevance_projection,
        )
        canonical_document = serialize_publication_document(document)
        canonical_bytes = ProductionArtifactStore.canonical_json_bytes(canonical_document)
        metadata = dict(metadata_extra or {})
        upstream_diagnostics = metadata.pop("diagnostics", {})
        metadata["diagnostics"] = {
            "warnings_by_stage": {
                "references": list(references.warnings),
                "extraction": list(extraction.warnings),
                "synthesis": list(synthesis.warnings),
                "editorial_enrichment": list(editorial_enrichment.warnings),
            },
            "upstream": (
                dict(upstream_diagnostics) if isinstance(upstream_diagnostics, Mapping) else {}
            ),
        }
        stage = ProductionArtifactStage.PUBLICATION
        current = await self._production_artifacts.get_current(run.id, stage.value)
        if (
            current is not None
            and current.input_hash == input_hash
            and current.status is ProductionArtifactStatus.VERIFIED
            and current.canonical_blob_id is not None
            and (
                metadata_extra is None
                or current.metadata.get("input_artifacts") == metadata_extra.get("input_artifacts")
            )
            and current.metadata.get("diagnostics") == metadata.get("diagnostics")
        ):
            try:
                stored = await self._artifact_store.read_bytes(current.canonical_blob_id)
                if stored == canonical_bytes:
                    return current
            except (BlobIntegrityError, EntityNotFoundError, FileNotFoundError):
                pass

        prior_versions = [
            artifact.version
            for artifact in await self._production_artifacts.list_for_run(run.id)
            if artifact.stage is stage
        ]
        version = max(prior_versions, default=0) + 1

        candidate = await self._reusable_candidate(run, input_hash, canonical_bytes)
        if candidate is not None:
            reused_metadata = dict(metadata)
            reused_metadata.update(
                {
                    "reused": True,
                    "reused_from_artifact_id": str(candidate.id),
                    "reused_from_created_at": candidate.created_at.isoformat(),
                }
            )
            artifact = ProductionArtifact(
                production_run_id=run.id,
                subject_id=run.subject_id,
                stage=stage,
                version=version,
                input_hash=input_hash,
                canonical_blob_id=candidate.canonical_blob_id,
                reused_from_artifact_id=candidate.id,
                metadata=reused_metadata,
            )
            self._set_repair_result_id(artifact)
            await self._production_artifacts.append(artifact)
            return artifact

        _, canonical_blob_id, _ = await self._artifact_store.store_stage_payloads(
            canonical=canonical_document
        )
        if canonical_blob_id is None:
            raise RuntimeError("Canonical publication body was not persisted")

        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=version,
            input_hash=input_hash,
            canonical_blob_id=canonical_blob_id,
            metadata=metadata,
        )
        self._set_repair_result_id(artifact)
        await self._production_artifacts.append(artifact)
        return artifact

    @staticmethod
    def _set_repair_result_id(artifact: ProductionArtifact) -> None:
        marker = artifact.metadata.get("repair_materialization")
        if isinstance(marker, dict):
            marker = dict(marker)
            marker["result_publication_artifact_id"] = str(artifact.id)
            artifact.metadata["repair_materialization"] = marker

    async def _reusable_candidate(
        self, run: ProductionRun, input_hash: str, canonical_bytes: bytes
    ) -> ProductionArtifact | None:
        candidate = await self._production_artifacts.find_reusable(
            edition_id=run.edition_id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.PUBLICATION.value,
            input_hash=input_hash,
        )
        if (
            candidate is None
            or candidate.stage is not ProductionArtifactStage.PUBLICATION
            or candidate.status is not ProductionArtifactStatus.VERIFIED
            or candidate.canonical_blob_id is None
        ):
            return None
        try:
            stored = await self._artifact_store.read_bytes(candidate.canonical_blob_id)
            if stored != canonical_bytes:
                return None
        except (BlobIntegrityError, EntityNotFoundError, FileNotFoundError):
            # An unreadable canonical body is not reusable; storage outages
            # still propagate as ProductionReuseStorageUnavailableError.
            return None
        return candidate

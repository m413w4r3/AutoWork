"""Read-only adapter that builds the planner context from pinned artifacts."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from cti_app.application.editorial_workspace.registry import WorkspaceSnapshotRecord
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentRevisionConflictError,
    EditorialEnrichmentValidationError,
    ProductionEditorialEnrichmentService,
    _EditorialEnrichmentInputControl,
)
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialSourceStage,
    EditorialWorkspaceError,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION,
    editorial_enrichment_from_json,
)

from .context import VisualRevisionContext, VisualRevisionRejected

logger = logging.getLogger(__name__)

_RESOLUTION_ARTIFACT_MISMATCH = "resolution:artifact_mismatch"
# Expected failures of the pinned read path. Anything else (AttributeError,
# KeyError, AssertionError, a refactor-induced NameError) is a programming
# error and must surface instead of becoming a silent 503 refusal.
_EXPECTED_LOAD_ERRORS = (
    ProductionReuseStorageUnavailableError,
    EditorialEnrichmentValidationError,
    _EditorialEnrichmentInputControl,
    SQLAlchemyError,
    TypeError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class VisualRevisionPreparation:
    """Pinned base state plus the planner context for one visual revision."""

    context: VisualRevisionContext
    base_artifact: ProductionArtifact
    entry: BlockRegistryEntry
    snapshot_id: UUID
    subject_id: UUID
    production_run_id: UUID
    base_canonical_sha256: str
    base_pin_hash: str
    routing_decision_hash: str | None


class VisualRevisionContextLoader:
    """Resolve one occurrence and load the exact pinned artifacts it edits."""

    def __init__(
        self,
        *,
        uow_factory: Any,
        artifact_store: ProductionArtifactStore,
        workspace_service: Any,
        enrichment_loader: ProductionEditorialEnrichmentService,
        enrichment_revision_service: ProductionEditorialEnrichmentRevisionService,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._workspace_service = workspace_service
        self._enrichment_loader = enrichment_loader
        self._enrichment_revision_service = enrichment_revision_service

    @property
    def diagram_preview_compiler(self) -> Any | None:
        """Compiler used for non-canonical diagram previews, when configured.

        Prepare never renders arbitrary code: it only asks this pinned compiler
        for the deterministic PNG preview of a typed ``DiagramSpecV1``.
        """
        return getattr(self._enrichment_loader, "_diagram_compiler", None)

    async def load(
        self,
        *,
        subject_id: UUID,
        snapshot_id: UUID,
        occurrence_id: UUID,
    ) -> VisualRevisionPreparation:
        try:
            resolution = await self._workspace_service.resolve_occurrence(subject_id, occurrence_id)
        except EditorialWorkspaceError:
            raise VisualRevisionRejected(
                "proposal_base_stale", {"reason": "workspace:occurrence_resolution"}
            ) from None
        if resolution.read_only_reason is not None:
            raise VisualRevisionRejected(
                "editorial_read_only", {"reason": resolution.read_only_reason.value}
            )
        record = resolution.record
        entry = resolution.entry
        if entry is None or resolution.binding_state != "valid":
            raise VisualRevisionRejected(
                "proposal_base_stale", {"reason": "workspace:occurrence_binding"}
            )
        if record.snapshot_id != snapshot_id:
            raise VisualRevisionRejected("proposal_base_stale", {"reason": "snapshot:id_mismatch"})
        if (
            record.enrichment_artifact_id is None
            or record.enrichment_version is None
            or record.enrichment_input_hash is None
            or record.enrichment_canonical_sha256 is None
        ):
            raise VisualRevisionRejected("proposal_base_unavailable", {})
        try:
            return await self._load_pinned(
                subject_id=subject_id,
                record=record,
                entry=entry,
                occurrence_id=occurrence_id,
            )
        except VisualRevisionRejected:
            raise
        except EditorialEnrichmentRevisionConflictError:
            raise VisualRevisionRejected(
                "proposal_base_stale", {"reason": "base:input_fence"}
            ) from None
        except _EXPECTED_LOAD_ERRORS as error:
            # Closed code + exception class only: never payload text.
            logger.warning(
                "visual_revision_base_unavailable code=%s exception_class=%s",
                "base:load_dependency",
                type(error).__name__,
            )
            raise VisualRevisionRejected("proposal_base_unavailable", {}) from None

    async def _load_pinned(
        self,
        *,
        subject_id: UUID,
        record: WorkspaceSnapshotRecord,
        entry: BlockRegistryEntry,
        occurrence_id: UUID,
    ) -> VisualRevisionPreparation:
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(subject_id)
            base_artifact = await uow.production_artifacts.get(record.enrichment_artifact_id)
            production_input = await uow.production_input_snapshots.get_by_run(
                record.production_run_id
            )
            current_enrichment = await uow.production_artifacts.get_current(
                record.production_run_id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
            )
            extraction_artifact = await uow.production_artifacts.get_current(
                record.production_run_id, ProductionArtifactStage.EXTRACTION.value
            )
            projection_artifact = await uow.production_artifacts.get_current(
                record.production_run_id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
            )
            synthesis_artifact = await uow.production_artifacts.get_current(
                record.production_run_id, ProductionArtifactStage.SYNTHESIS.value
            )
        if run is None or run.id != record.production_run_id:
            raise VisualRevisionRejected("proposal_base_stale", {"reason": "run:not_current"})
        if production_input is None or production_input.production_run_id != run.id:
            raise VisualRevisionRejected("proposal_base_stale", {"reason": "snapshot:missing"})
        if (
            base_artifact is None
            or base_artifact.id != record.enrichment_artifact_id
            or base_artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT
            or base_artifact.status is not ProductionArtifactStatus.VERIFIED
            or base_artifact.canonical_blob_id is None
            or base_artifact.version != record.enrichment_version
            or base_artifact.input_hash != record.enrichment_input_hash
        ):
            raise VisualRevisionRejected(
                "proposal_base_stale", {"reason": "pins:enrichment_artifact"}
            )
        if current_enrichment is None or current_enrichment.id != base_artifact.id:
            raise VisualRevisionRejected(
                "proposal_base_stale", {"reason": "pins:current_artifact_id"}
            )
        if extraction_artifact is None or synthesis_artifact is None:
            raise VisualRevisionRejected("proposal_base_unavailable", {})
        if entry.block_id != occurrence_id:
            raise VisualRevisionRejected(
                "proposal_base_stale", {"reason": _RESOLUTION_ARTIFACT_MISMATCH}
            )
        if entry.source_stage is EditorialSourceStage.SYNTHESIS and (
            synthesis_artifact.version != record.synthesis_version
            or synthesis_artifact.id != record.synthesis_artifact_id
            or record.synthesis_canonical_sha256 is None
        ):
            raise VisualRevisionRejected(
                "proposal_base_stale", {"reason": "pins:synthesis_artifact"}
            )

        extraction = await self._enrichment_loader._load_extraction(
            run, production_input, extraction_artifact
        )
        projection = (
            await self._enrichment_loader._load_projection(
                run, production_input, extraction, projection_artifact
            )
            if projection_artifact is not None
            else None
        )
        synthesis = await self._enrichment_loader._load_synthesis(
            run, production_input, synthesis_artifact, extraction
        )
        fence = await self._enrichment_revision_service._validate_input_fence(
            snapshot=production_input,
            base_artifact=base_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            extraction=extraction,
            projection=projection,
            synthesis=synthesis,
            read_only_media=True,
        )
        base_payload = await self._artifact_store.read_json(base_artifact.canonical_blob_id)
        if not isinstance(base_payload, dict):
            raise VisualRevisionRejected("proposal_base_unavailable", {})
        canonical_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(base_payload)
        ).hexdigest()
        if canonical_sha256 != record.enrichment_canonical_sha256:
            raise VisualRevisionRejected("proposal_base_stale", {"reason": "base:canonical_sha256"})
        try:
            base_enrichment = editorial_enrichment_from_json(base_payload)
        except (TypeError, ValueError):
            raise VisualRevisionRejected(
                "proposal_base_stale", {"reason": "base:enrichment_payload"}
            ) from None
        if base_enrichment.schema_version > EDITORIAL_ENRICHMENT_MAX_SCHEMA_VERSION:
            raise VisualRevisionRejected("proposal_base_stale", {"reason": "base:schema_version"})
        context = VisualRevisionContext(
            base_enrichment=base_enrichment,
            evidence_pack=fence.evidence_pack,
            extraction=extraction,
            synthesis=synthesis,
            figure_catalog=tuple(fence.figure_catalog),
            registered_anchor_ids=fence.evidence_pack.registered_anchor_ids,
            production_run_id=record.production_run_id,
            publication_kind=record.publication_kind,
        )
        return VisualRevisionPreparation(
            context=context,
            base_artifact=base_artifact,
            entry=entry,
            snapshot_id=record.snapshot_id,
            subject_id=record.subject_id,
            production_run_id=record.production_run_id,
            base_canonical_sha256=record.enrichment_canonical_sha256,
            base_pin_hash=record.workspace_pin_hash,
            routing_decision_hash=record.routing_decision_hash,
        )

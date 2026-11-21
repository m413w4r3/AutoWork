"""Versioned, single-element Editorial Enrichment revisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application import editorial_enrichment_revision as enrichment_revision_rules
from cti_app.application.editorial_enrichment_candidate import (
    build_candidate_from_validated_patch,
    materialize_candidate_diagrams,
    persist_candidate,
)
from cti_app.application.editorial_enrichment_revision import (
    EditorialEnrichmentRevisionLineage,
    EditorialEnrichmentRevisionValidationError,
    requested_registered_placement,
)
from cti_app.application.editorial_revision_effects import (
    EditorialRevisionEffects,
    EditorialRevisionEffectsError,
)
from cti_app.application.editorial_workspace.registry import WorkspaceSnapshotRecord
from cti_app.application.media_assets import compile_and_store_diagrams
from cti_app.application.model_gateway import ModelSubmissionReconciliationRequiredError
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION,
    EditorialEnrichmentEvidencePackV1,
    EditorialEnrichmentProposalControlError,
    EditorialEnrichmentRevisionConflictError,
    EditorialEnrichmentWireParseResult,
    ProductionEditorialEnrichmentService,
    build_editorial_enrichment_evidence_pack,
    build_editorial_enrichment_model_request,
    build_editorial_figure_catalog,
    build_source_figure_provenance_diagnostics,
    canonical_synthesis_hash,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
    editorial_enrichment_output_contract_example,
    parse_editorial_enrichment_proposal_wire,
    revalidate_editorial_enrichment_quality,
    validate_editorial_enrichment,
    validate_editorial_enrichment_proposal,
)
from cti_app.application.production_prompts import (
    EDITORIAL_ENRICHMENT_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
)
from cti_app.application.production_references import production_reference_corpus_from_json
from cti_app.application.production_repairs import _effective_decisions_for_reader
from cti_app.application.production_routing import get_adopted_production_routing_decision
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
)
from cti_app.application.production_synthesis_revision import (
    TargetedSynthesisRevisionPreview,
    TargetedSynthesisRevisionResult,
    TargetedSynthesisRevisionValidationError,
    _critical_extraction_assessment,
    _targeted_revision_transport_error,
)
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionStage,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EditorialEnrichmentRevisionAction,
    EditorialEnrichmentRevisionOutcome,
    EditorialEnrichmentV1,
    EnrichmentPlacementKind,
    editorial_enrichment_evidence_refs,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRefusal,
    TargetedEditorialRevisionRefusalCode,
    TargetedEditorialRevisionRefusalReason,
    TargetedEditorialRevisionRequest,
)
from cti_app.domain.production_synthesis import (
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    production_synthesis_from_json,
    production_synthesis_to_json,
)
from cti_app.integrations.models import BridgeTransportError

MAX_EDITORIAL_REVISION_INSTRUCTION_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentRevisionResult:
    artifact: ProductionArtifact
    outcome: EditorialEnrichmentRevisionOutcome
    revision: dict[str, Any]
    publication_artifact: ProductionArtifact | None
    previous_publication_artifact_id: UUID | None


@dataclass(frozen=True, slots=True)
class StaleEnrichmentRestorePreview:
    base_artifact_id: UUID
    base_version: int
    base_input_hash: str
    base_canonical_sha256: str
    restorable_blocks: tuple[dict[str, Any], ...]
    non_restorable_blocks: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class StaleEnrichmentRestoreResult:
    artifact: ProductionArtifact
    revision: dict[str, Any]
    publication_artifact: ProductionArtifact
    restorable_blocks: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _EditorialEnrichmentRevisionFence:
    access_policy: Any
    inventory: Any
    figure_catalog: tuple[Any, ...]
    evidence_pack: Any
    evidence_pack_hash: str
    inventory_hash: str | None
    input_hash: str
    workspace_record: Any


class _ReadOnlyWorkspaceSnapshotRepository:
    """Let workspace readers build an ephemeral snapshot without appending it."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)

    async def append_if_absent(self, record: WorkspaceSnapshotRecord) -> WorkspaceSnapshotRecord:
        existing = await self._repository.get_by_pin(
            record.production_run_id, record.workspace_pin_hash
        )
        return cast(WorkspaceSnapshotRecord, existing or record)


class _ReadOnlyWorkspaceSnapshotUow:
    """Proxy one UOW while making its snapshot repository read-only."""

    def __init__(self, uow: Any) -> None:
        self._uow = uow
        self.editorial_workspace_snapshots = _ReadOnlyWorkspaceSnapshotRepository(
            uow.editorial_workspace_snapshots
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._uow, name)


class _StoredSourceMediaArchiver:
    """Expose already persisted media candidates without collecting new media."""

    def __init__(self, uow_factory: Any, delegate: Any) -> None:
        self._uow_factory = uow_factory
        self._delegate = delegate

    @property
    def policy_sha256(self) -> str:
        return cast(str, self._delegate.policy_sha256)

    async def collect(self, subject_id: UUID, sources: tuple[Any, ...]) -> tuple[Any, ...]:
        del sources
        async with self._uow_factory() as uow:
            return tuple(
                await uow.source_media_candidates.list_for_subject_policy(
                    subject_id, self.policy_sha256
                )
            )


class ProductionEditorialEnrichmentRevisionService:
    """Draft one replacement element against the exact proofs admitted by its base."""

    def __init__(
        self,
        *,
        enrichment_service: ProductionEditorialEnrichmentService,
        persistence_service: EditorialEnrichmentService,
        publication_render_service: PublicationRenderService,
        revision_effects: EditorialRevisionEffects | None = None,
    ) -> None:
        self._enrichment = enrichment_service
        self._lineage = EditorialEnrichmentRevisionLineage(enrichment_service)
        self._persistence = persistence_service
        self._publication_render_service = publication_render_service
        self._revision_effects = revision_effects

    async def _critical_source_assessment(
        self,
        context: tuple[Any, ...],
        extraction: Any,
        references: Any,
    ) -> tuple[tuple[Any, ...], tuple[dict[str, Any], ...]]:
        run = context[0]
        references_artifact = context[5]
        extraction_artifact = context[6]
        async with self._enrichment._uow_factory() as uow:
            effective_decisions = await _effective_decisions_for_reader(
                uow, run.edition_id, run.subject_id
            )
        return _critical_extraction_assessment(
            extraction,
            references,
            run=run,
            extraction_artifact=extraction_artifact,
            references_artifact=references_artifact,
            effective_repair_decisions=effective_decisions,
        )

    async def _validate_input_fence(
        self,
        *,
        snapshot: ProductionInputSnapshot,
        base_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        projection_artifact: ProductionArtifact | None,
        synthesis_artifact: ProductionArtifact,
        extraction: Any,
        projection: Any,
        synthesis: Any,
        read_only_media: bool = False,
        read_only_snapshot: bool = False,
    ) -> _EditorialEnrichmentRevisionFence:
        media_archiver: Any = self._enrichment._source_media_archiver
        if read_only_media and media_archiver is not None:
            media_archiver = _StoredSourceMediaArchiver(
                self._enrichment._uow_factory, media_archiver
            )
        async with self._enrichment._uow_factory() as uow:
            access_policy = await build_synthesis_access_policy(
                snapshot,
                extraction,
                uow.source_documents,
                uow.source_collections,
            )
            inventory = await load_archived_source_figure_inventory(
                subject_id=snapshot.subject_id,
                extraction_sources=extraction.sources,
                source_document_repository=uow.source_documents,
                blob_repository=uow.blobs,
                artifact_store=self._enrichment._artifact_store,
                media_archiver=media_archiver,
            )
            from cti_app.application.editorial_workspace.service import (
                EditorialWorkspaceService,
            )

            # Preview prechecks once appended missing snapshots; keep this read path append-free.
            snapshot_uow = _ReadOnlyWorkspaceSnapshotUow(uow) if read_only_snapshot else uow
            workspace_record = await EditorialWorkspaceService(
                uow_factory=cast(Any, self._enrichment._uow_factory),
                artifact_store=self._enrichment._artifact_store,
            ).snapshot_record_in_uow(cast(Any, snapshot_uow), snapshot.subject_id)
            if workspace_record.synthesis_artifact_id != synthesis_artifact.id:
                raise EditorialEnrichmentRevisionConflictError(
                    "The pinned synthesis no longer matches the requested revision base"
                )

        lineage_rebased = base_artifact.metadata.get("lineage_rebased") is True
        extraction_hash = canonical_extraction_hash(extraction)
        synthesis_hash = canonical_synthesis_hash(synthesis)
        projection_hash = projection.projection_hash if projection is not None else None
        if lineage_rebased and not self._rebased_lineage_matches(
            base_artifact=base_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            extraction_hash=extraction_hash,
            synthesis_hash=synthesis_hash,
            projection_hash=projection_hash,
        ):
            raise EditorialEnrichmentRevisionConflictError(
                "The rebased input lineage no longer matches the base artifact"
            )

        if synthesis_access_policy_hash(access_policy) != base_artifact.metadata.get(
            "access_policy_hash"
        ):
            raise EditorialEnrichmentRevisionConflictError(
                "The access policy no longer matches the base artifact"
            )

        if base_artifact.canonical_blob_id is None:
            raise EditorialEnrichmentRevisionConflictError(
                "The base enrichment artifact has no canonical payload"
            )
        base_enrichment = editorial_enrichment_from_json(
            await self._enrichment._artifact_store.read_json(base_artifact.canonical_blob_id)
        )

        try:
            evidence_pack = build_editorial_enrichment_evidence_pack(
                snapshot, extraction, synthesis, projection, workspace_record
            )
        except ValueError as exc:
            raise EditorialEnrichmentRevisionConflictError(
                "The editorial workspace registry does not match the current synthesis"
            ) from exc
        evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
        legacy_evidence_pack = (
            build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis, projection)
            if base_enrichment.schema_version < 9
            else evidence_pack
        )
        if not lineage_rebased and base_artifact.metadata.get("evidence_pack_hash") != (
            editorial_enrichment_evidence_pack_hash(legacy_evidence_pack)
        ):
            raise EditorialEnrichmentRevisionConflictError(
                "The admitted evidence pack no longer matches the base artifact"
            )

        if (
            lineage_rebased
            and base_artifact.metadata.get("source_figure_inventory_hash")
            != inventory.functional_hash()
        ):
            raise EditorialEnrichmentRevisionConflictError(
                "The admitted figure catalog no longer matches the base artifact"
            )
        figure_catalog, inventory_hash = self._catalog_for_base_artifact(
            base_artifact=base_artifact,
            extraction=extraction,
            inventory=inventory,
        )
        input_hash = compute_editorial_enrichment_input_hash(
            extraction=extraction,
            synthesis=synthesis,
            evidence_pack_hash=evidence_pack_hash,
            access_policy_hash=base_artifact.metadata["access_policy_hash"],
            projection_hash=projection_hash,
            source_figure_inventory_hash=inventory_hash,
            resource_search_enabled=self._enrichment._resource_search_enabled,
        )
        if input_hash != base_artifact.input_hash and not lineage_rebased:
            if not self._legacy_input_fence_matches(
                base_artifact=base_artifact,
                extraction=extraction,
                synthesis=synthesis,
                projection=projection,
                resource_search_enabled=self._enrichment._resource_search_enabled,
            ):
                raise EditorialEnrichmentRevisionConflictError(
                    "The Editorial Enrichment inputs no longer match the base artifact"
                )

        return _EditorialEnrichmentRevisionFence(
            access_policy=access_policy,
            inventory=inventory,
            figure_catalog=figure_catalog,
            evidence_pack=evidence_pack,
            evidence_pack_hash=evidence_pack_hash,
            inventory_hash=inventory_hash,
            input_hash=input_hash,
            workspace_record=workspace_record,
        )

    @staticmethod
    def _rebased_lineage_matches(
        *,
        base_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        projection_artifact: ProductionArtifact | None,
        synthesis_artifact: ProductionArtifact,
        extraction_hash: str,
        synthesis_hash: str,
        projection_hash: str | None,
    ) -> bool:
        return enrichment_revision_rules.rebased_lineage_matches(
            base_artifact=base_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            extraction_hash=extraction_hash,
            synthesis_hash=synthesis_hash,
            projection_hash=projection_hash,
        )

    @staticmethod
    def _legacy_input_fence_matches(
        *,
        base_artifact: ProductionArtifact,
        extraction: Any,
        synthesis: Any,
        projection: Any,
        resource_search_enabled: bool,
    ) -> bool:
        return enrichment_revision_rules.legacy_input_fence_matches(
            base_artifact=base_artifact,
            extraction=extraction,
            synthesis=synthesis,
            projection=projection,
            resource_search_enabled=resource_search_enabled,
        )

    @staticmethod
    def _catalog_for_base_artifact(
        *, base_artifact: ProductionArtifact, extraction: Any, inventory: Any
    ) -> tuple[tuple[Any, ...], str | None]:
        stored_hash = base_artifact.metadata.get("source_figure_inventory_hash")
        current_hash = inventory.functional_hash()
        if stored_hash is None or stored_hash == current_hash:
            return build_editorial_figure_catalog(extraction, inventory), current_hash

        conflict_message = "The admitted figure catalog no longer matches the base artifact"
        diagnostics = base_artifact.metadata.get("source_figure_provenance_diagnostics")
        if (
            base_artifact.metadata.get("source_figure_provenance_version")
            != SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION
            or not isinstance(stored_hash, str)
            or not isinstance(diagnostics, list)
        ):
            raise EditorialEnrichmentRevisionConflictError(conflict_message)

        def diagnostic_signature(records: list[Any], figure_id: str) -> list[tuple[str, ...]]:
            signature: list[tuple[str, ...]] = []
            for record in records:
                if not isinstance(record, dict) or record.get("figure_id") != figure_id:
                    continue
                stage = record.get("stage")
                if stage in {"catalogued", "selected", "published"}:
                    continue
                if not isinstance(stage, str):
                    raise EditorialEnrichmentRevisionConflictError(conflict_message)
                signature.append(
                    (
                        stage,
                        str(record.get("source_document_id") or ""),
                        str(record.get("blob_id") or ""),
                        str(record.get("sha256") or ""),
                        str(record.get("reason_code") or ""),
                    )
                )
            return sorted(signature)

        admitted: dict[str, dict[str, Any]] = {}
        discovered_ids: set[str] = set()
        for record in diagnostics:
            if not isinstance(record, dict):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)
            figure_id = record.get("figure_id")
            stage = record.get("stage")
            if stage == "discovered":
                if not isinstance(figure_id, str):
                    raise EditorialEnrichmentRevisionConflictError(conflict_message)
                discovered_ids.add(figure_id)
            if stage != "catalogued":
                continue
            if (
                not isinstance(figure_id, str)
                or not isinstance(record.get("source_document_id"), str)
                or not isinstance(record.get("figure_handle"), str)
                or figure_id in admitted
            ):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)
            admitted[figure_id] = record

        current_by_id = {str(figure.figure_id): figure for figure in inventory.figures}
        if not set(admitted).issubset(discovered_ids) or not set(admitted).issubset(current_by_id):
            raise EditorialEnrichmentRevisionConflictError(conflict_message)
        for figure_id in admitted:
            if diagnostic_signature(diagnostics, figure_id) != diagnostic_signature(
                [record.to_json() for record in inventory.provenance_diagnostics], figure_id
            ):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)

        admitted_ids = set(admitted)
        scoped_inventory = replace(
            inventory,
            figures=tuple(
                figure for figure in inventory.figures if str(figure.figure_id) in admitted_ids
            ),
            catalog_metadata={
                figure_id: metadata
                for figure_id, metadata in inventory.catalog_metadata.items()
                if str(figure_id) in admitted_ids
            },
        )
        figure_catalog = build_editorial_figure_catalog(extraction, scoped_inventory)
        current_catalog = {str(entry.figure.figure_id): entry for entry in figure_catalog}
        if set(current_catalog) != admitted_ids:
            raise EditorialEnrichmentRevisionConflictError(conflict_message)

        for figure_id, expected in admitted.items():
            entry = current_catalog[figure_id]
            figure = entry.figure
            expected_blob_id = expected.get("blob_id")
            expected_sha256 = expected.get("sha256")
            if (
                entry.handle != expected["figure_handle"]
                or str(figure.source_document_id) != expected["source_document_id"]
                or (expected_blob_id is None) != (expected_sha256 is None)
            ):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)
            if expected_blob_id is not None:
                if (
                    figure.decision.value != "accepted"
                    or str(figure.blob_id) != expected_blob_id
                    or figure.sha256 != expected_sha256
                ):
                    raise EditorialEnrichmentRevisionConflictError(conflict_message)
            elif figure.decision.value == "accepted" or figure.decision_reason != expected.get(
                "reason_code"
            ):
                raise EditorialEnrichmentRevisionConflictError(conflict_message)

        # Legacy provenance v1 stores per-figure events but not a snapshot of the
        # inventory hash inputs. Keep the base catalog fenced: every admitted
        # figure and its archive decision must remain unchanged.
        return figure_catalog, stored_hash

    async def preview_targeted(
        self, request: TargetedEditorialRevisionRequest
    ) -> TargetedSynthesisRevisionPreview:
        self._validate_targeted_request(request)
        try:
            context = await self._load_context(
                subject_id=request.subject_id,
                base_artifact_id=request.base_artifact_id,
                base_version=request.base_version,
                base_input_hash=request.base_input_hash,
                base_canonical_sha256=request.base_canonical_sha256,
            )
        except EditorialEnrichmentRevisionConflictError as exc:
            from cti_app.domain.production_editorial_revision import (
                TargetedEditorialRevisionConflictError,
            )

            raise TargetedEditorialRevisionConflictError(str(exc)) from exc
        (
            run,
            snapshot,
            base_artifact,
            _base_payload,
            _base_enrichment,
            references_artifact,
            extraction_artifact,
            projection_artifact,
            synthesis_artifact,
            _previous_publication,
        ) = context
        extraction = await self._enrichment._load_extraction(run, snapshot, extraction_artifact)
        projection = (
            await self._enrichment._load_projection(run, snapshot, extraction, projection_artifact)
            if projection_artifact is not None
            else None
        )
        synthesis = await self._enrichment._load_synthesis(
            run, snapshot, synthesis_artifact, extraction
        )
        try:
            await self._validate_input_fence(
                snapshot=snapshot,
                base_artifact=base_artifact,
                extraction_artifact=extraction_artifact,
                projection_artifact=projection_artifact,
                synthesis_artifact=synthesis_artifact,
                extraction=extraction,
                projection=projection,
                synthesis=synthesis,
                read_only_media=True,
                read_only_snapshot=True,
            )
        except EditorialEnrichmentRevisionConflictError as exc:
            from cti_app.domain.production_editorial_revision import (
                TargetedEditorialRevisionConflictError,
            )

            raise TargetedEditorialRevisionConflictError(str(exc)) from exc
        identity = self._targeted_request_identity(request)
        operation = request.operation
        is_delete = operation in {
            TargetedEditorialRevisionOperation.DELETE_TABLE,
            TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
            TargetedEditorialRevisionOperation.DELETE_CHART,
            TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
        }
        calls = 0
        if request.mode is TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY and not is_delete:
            references_blob_id = references_artifact.canonical_blob_id
            if references_blob_id is None:
                raise EditorialEnrichmentRevisionConflictError(
                    "The current References artifact has no canonical payload"
                )
            references = production_reference_corpus_from_json(
                await self._enrichment._artifact_store.read_json(references_blob_id)
            )
            critical_failures, _ = await self._critical_source_assessment(
                context, extraction, references
            )
            if not critical_failures:
                run_id = uuid5(
                    NAMESPACE_URL,
                    f"production-editorial-enrichment-revision-model:{identity}",
                )
                calls = 0 if await self._enrichment._model_gateway.get_run(run_id) else 1
        target = request.target
        kind = target.element_kind.value if target.element_kind is not None else "unknown"
        key = target.element_key or "new"
        return TargetedSynthesisRevisionPreview(
            base_artifact_id=base_artifact.id,
            base_version=base_artifact.version,
            base_input_hash=base_artifact.input_hash,
            base_canonical_sha256=request.base_canonical_sha256,
            target_path=f"editorial_enrichment.{kind}[{key}]",
            stages_preserved=(
                ProductionArtifactStage.REFERENCES.value,
                ProductionArtifactStage.EXTRACTION.value,
                ProductionArtifactStage.RELEVANCE_PROJECTION.value,
                ProductionArtifactStage.SYNTHESIS.value,
            ),
            stages_invalidated=(ProductionArtifactStage.PUBLICATION.value,),
            stages_conditionally_rebased=(),
            source_extension=(request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION),
            expected_model_calls=calls,
            enrichment_rebase="not_applicable; only the targeted enrichment artifact changes",
        )

    async def revise_targeted(
        self, request: TargetedEditorialRevisionRequest
    ) -> TargetedSynthesisRevisionResult:
        self._validate_targeted_request(request)
        assert request.target.element_kind is not None
        action = self._targeted_action(request.operation)
        try:
            result = await self.revise(
                subject_id=request.subject_id,
                base_artifact_id=request.base_artifact_id,
                base_version=request.base_version,
                base_input_hash=request.base_input_hash,
                base_canonical_sha256=request.base_canonical_sha256,
                element_kind=request.target.element_kind,
                element_key=request.target.element_key,
                action=action,
                instruction=request.instruction,
                targeted_request=request,
            )
            return cast(TargetedSynthesisRevisionResult, result)
        except EditorialEnrichmentRevisionConflictError as exc:
            from cti_app.domain.production_editorial_revision import (
                TargetedEditorialRevisionConflictError,
            )

            raise TargetedEditorialRevisionConflictError(str(exc)) from exc
        except EditorialEnrichmentRevisionValidationError as exc:
            raise TargetedSynthesisRevisionValidationError(
                "editorial_enrichment_revision_rejected",
                details={"rejections": exc.rejections},
            ) from exc

    async def preview_restore_stale_enrichment_blocks(
        self,
        *,
        subject_id: UUID,
        base_artifact_id: UUID,
        base_version: int,
        base_input_hash: str,
        base_canonical_sha256: str,
    ) -> StaleEnrichmentRestorePreview:
        context = await self._load_context(
            subject_id=subject_id,
            base_artifact_id=base_artifact_id,
            base_version=base_version,
            base_input_hash=base_input_hash,
            base_canonical_sha256=base_canonical_sha256,
        )
        evaluation = await self._evaluate_stale_enrichment_restore(context, read_only_snapshot=True)
        base_artifact = context[2]
        return StaleEnrichmentRestorePreview(
            base_artifact_id=base_artifact.id,
            base_version=base_artifact.version,
            base_input_hash=base_artifact.input_hash,
            base_canonical_sha256=base_canonical_sha256,
            restorable_blocks=tuple(evaluation[1]),
            non_restorable_blocks=tuple(evaluation[2]),
        )

    async def restore_stale_enrichment_blocks(
        self,
        *,
        subject_id: UUID,
        base_artifact_id: UUID,
        base_version: int,
        base_input_hash: str,
        base_canonical_sha256: str,
        actor: str,
    ) -> StaleEnrichmentRestoreResult:
        context = await self._load_context(
            subject_id=subject_id,
            base_artifact_id=base_artifact_id,
            base_version=base_version,
            base_input_hash=base_input_hash,
            base_canonical_sha256=base_canonical_sha256,
        )
        evaluation = await self._evaluate_stale_enrichment_restore(context)
        (
            run,
            snapshot,
            base_artifact,
            base_payload,
            _context_enrichment,
            references_artifact,
            extraction_artifact,
            projection_artifact,
            synthesis_artifact,
            previous_publication,
        ) = context
        # The canonical payload is the source of truth for restore content.
        base_enrichment = editorial_enrichment_from_json(base_payload)
        restored_enrichment, restored_blocks, non_restorable = evaluation
        if not restored_blocks:
            raise EditorialEnrichmentRevisionValidationError(
                [
                    {
                        "block_id": "restore",
                        "reason_code": "no_stale_enrichment_blocks_to_restore",
                    }
                ]
            )

        extraction = await self._enrichment._load_extraction(run, snapshot, extraction_artifact)
        projection = (
            await self._enrichment._load_projection(run, snapshot, extraction, projection_artifact)
            if projection_artifact is not None
            else None
        )
        synthesis = await self._enrichment._load_synthesis(
            run, snapshot, synthesis_artifact, extraction
        )
        fence = await self._validate_input_fence(
            snapshot=snapshot,
            base_artifact=base_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            extraction=extraction,
            projection=projection,
            synthesis=synthesis,
            read_only_media=True,
        )
        restore_evidence_pack_hash = fence.evidence_pack_hash
        restore_input_hash = fence.input_hash
        restore_access_policy_hash = synthesis_access_policy_hash(fence.access_policy)
        if restored_enrichment.schema_version < 9:
            legacy_evidence_pack = build_editorial_enrichment_evidence_pack(
                snapshot, extraction, synthesis, projection
            )
            restore_evidence_pack_hash = editorial_enrichment_evidence_pack_hash(
                legacy_evidence_pack
            )
            restore_input_hash = compute_editorial_enrichment_input_hash(
                extraction=extraction,
                synthesis=synthesis,
                evidence_pack_hash=restore_evidence_pack_hash,
                access_policy_hash=restore_access_policy_hash,
                projection_hash=projection.projection_hash if projection is not None else None,
                source_figure_inventory_hash=fence.inventory_hash,
                resource_search_enabled=self._enrichment._resource_search_enabled,
            )

        before_refs = editorial_enrichment_evidence_refs(base_enrichment)
        restored_refs = editorial_enrichment_evidence_refs(restored_enrichment) - before_refs
        handles_by_ref = {ref: handle for handle, ref in fence.evidence_pack._handle_to_ref.items()}
        evidence_handles = sorted(
            handles_by_ref[ref] for ref in restored_refs if ref in handles_by_ref
        )
        evidence_refs = [
            {
                "handle": handles_by_ref[ref],
                "ref": {
                    "source_document_id": str(ref.source_document_id),
                    "kind": ref.kind.value,
                    "evidence_key": ref.evidence_key,
                },
            }
            for ref in sorted(
                restored_refs,
                key=lambda item: (str(item.source_document_id), item.kind.value, item.evidence_key),
            )
            if ref in handles_by_ref
        ]
        restored_identities = [dict(item) for item in restored_blocks]
        request_identity_payload = {
            "subject_id": str(subject_id),
            "base_artifact_id": str(base_artifact.id),
            "base_version": base_artifact.version,
            "base_input_hash": base_artifact.input_hash,
            "base_canonical_sha256": base_canonical_sha256,
            "operation": "restore_enrichment_blocks",
            "actor": actor,
            "restored_blocks": restored_identities,
            "contract_version": "restore-stale-enrichment-v1",
        }
        request_identity = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(request_identity_payload)
        ).hexdigest()
        revision_artifact_id = uuid5(
            NAMESPACE_URL, f"targeted-editorial-enrichment-restore:{request_identity}"
        )
        final_payload = editorial_enrichment_to_json(restored_enrichment)
        revision_data = {
            "revision_id": str(revision_artifact_id),
            "request_identity": request_identity,
            "base_artifact_id": str(base_artifact.id),
            "base_version": base_artifact.version,
            "base_artifact_version": base_artifact.version,
            "base_input_hash": base_artifact.input_hash,
            "base_artifact_hash": base_canonical_sha256,
            "base_canonical_sha256": base_canonical_sha256,
            "target": {"kind": "editorial_enrichment", "block_keys": restored_identities},
            "target_path": "editorial_enrichment.restore",
            "operation": "restore_enrichment_blocks",
            "outcome": "restored",
            "request": "Restore verified stale enrichment blocks",
            "mode": TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY.value,
            "actor": actor,
            "model_run_id": None,
            "prompt_version": "none",
            "contract_version": "restore-stale-enrichment-v1",
            "parser_version": "not_applicable",
            "validator_version": "editorial-enrichment-quality-validator-v1",
            "validator_results": [
                {"validator": "enrichment_anchor_validation", "status": "passed"},
                {"validator": "current_evidence_pack", "status": "passed"},
                {"validator": "editorial_enrichment_quality", "status": "passed"},
            ],
            "validator_rejections": [],
            "evidence_refs": evidence_refs,
            "evidence_handles": evidence_handles,
            "source_additions": [],
            "before_hash": base_canonical_sha256,
            "after_hash": hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(final_payload)
            ).hexdigest(),
            "rationale": "Blocks restored from verified enrichment history after revalidation.",
            "created_at": datetime.now(UTC).isoformat(),
            "result_artifact_id": str(revision_artifact_id),
            "restored_blocks": restored_identities,
            "non_restorable_blocks": [dict(item) for item in non_restorable],
            "model_call_count": 0,
            "previous_publication_artifact_id": (
                str(previous_publication.id) if previous_publication is not None else None
            ),
        }

        async def persist_restore(uow: Any | None = None) -> ProductionArtifact:
            return await self._persistence.store_editorial_enrichment_result(
                run_id=run.id,
                subject_id=subject_id,
                input_hash=restore_input_hash,
                enrichment=restored_enrichment,
                extraction=extraction,
                synthesis=synthesis,
                raw_result=None,
                model_run_id=None,
                evidence_pack_hash=restore_evidence_pack_hash,
                access_policy_hash=restore_access_policy_hash,
                model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
                routing_policy_version=str(
                    base_artifact.metadata.get("routing_policy_version", "")
                ),
                projection_hash=projection.projection_hash if projection is not None else None,
                source_figure_inventory_hash=fence.inventory_hash,
                metadata_extra={
                    **(
                        {
                            "generator_version": (
                                enrichment_revision_rules._LEGACY_EDITORIAL_ENRICHMENT_GENERATOR_VERSION
                            )
                        }
                        if restored_enrichment.schema_version < 9
                        else {}
                    ),
                    "source_figure_provenance_version": (
                        SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION
                    ),
                    "source_figure_provenance_diagnostics": (
                        build_source_figure_provenance_diagnostics(
                            fence.inventory, fence.figure_catalog, restored_enrichment
                        )
                    ),
                    "revision": revision_data,
                    "revision_restore": {
                        "from_artifact_id": str(base_artifact.id),
                        "restored_blocks": restored_identities,
                        "non_restorable_blocks": [dict(item) for item in non_restorable],
                        "model_call_count": 0,
                    },
                },
                artifact_id=revision_artifact_id,
                expected_current_artifact_id=base_artifact.id,
                expected_current_input_hash=base_artifact.input_hash,
                expected_current_canonical_sha256=base_canonical_sha256,
                canonical_payload=final_payload,
                resource_search_enabled=self._enrichment._resource_search_enabled,
                uow=uow,
            )

        if self._revision_effects is None:
            artifact = await persist_restore()
        else:
            if not actor.strip():
                raise EditorialEnrichmentRevisionConflictError("editorial_revision_actor_invalid")
            revision_actor_id = actor
            try:
                async with self._enrichment._uow_factory() as uow:
                    run_guard = await self._revision_effects.acquire_run_guard(uow, run.id)
                    await self._revision_effects.lock_run_row(uow, run_guard)
                    current = await uow.production_artifacts.get_current(
                        run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
                    )
                    if current is None or current.id != base_artifact.id:
                        raise EditorialEnrichmentRevisionConflictError(
                            "The requested base artifact is stale"
                        )
                    base_workspace_snapshot = await self._revision_effects.current_snapshot_in_uow(
                        uow, subject_id
                    )
                    if base_workspace_snapshot.production_run_id != run.id:
                        raise EditorialEnrichmentRevisionConflictError(
                            "The requested production run is no longer current"
                        )
                    if base_workspace_snapshot.synthesis_artifact_id != synthesis_artifact.id:
                        raise EditorialEnrichmentRevisionConflictError(
                            "The requested synthesis base is no longer current"
                        )
                    artifact = await persist_restore(uow)
                    await self._revision_effects.apply_after_write(
                        uow,
                        run_guard=run_guard,
                        base_snapshot=base_workspace_snapshot,
                        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                        artifact=artifact,
                        canonical_sha256=hashlib.sha256(
                            ProductionArtifactStore.canonical_json_bytes(final_payload)
                        ).hexdigest(),
                        canonical_content=final_payload,
                        lineage=None,
                        actor_id=revision_actor_id,
                    )
                    await uow.commit()
            except EditorialRevisionEffectsError as error:
                raise EditorialEnrichmentRevisionConflictError(error.code) from None
        references_blob_id = references_artifact.canonical_blob_id
        if references_blob_id is None:
            raise EditorialEnrichmentRevisionConflictError(
                "The current References artifact has no canonical payload"
            )
        references = production_reference_corpus_from_json(
            await self._enrichment._artifact_store.read_json(references_blob_id)
        )
        publication = await self._assemble_and_render(
            run=run,
            snapshot=snapshot,
            references_artifact=references_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            enrichment_artifact=artifact,
            references=references,
            extraction=extraction,
            projection=projection,
            synthesis=synthesis,
            enrichment=restored_enrichment,
        )
        return StaleEnrichmentRestoreResult(
            artifact=artifact,
            revision=revision_data,
            publication_artifact=publication,
            restorable_blocks=tuple(restored_identities),
        )

    async def _evaluate_stale_enrichment_restore(
        self, context: tuple[Any, ...], *, read_only_snapshot: bool = False
    ) -> tuple[EditorialEnrichmentV1, list[dict[str, Any]], list[dict[str, Any]]]:
        from cti_app.application.production_synthesis_revision import (
            _enrichment_section_anchor_id,
        )
        from cti_app.domain.production import ProductionArtifactStatus

        (
            run,
            snapshot,
            base_artifact,
            base_payload,
            _context_enrichment,
            _references_artifact,
            extraction_artifact,
            projection_artifact,
            synthesis_artifact,
            _previous_publication,
        ) = context
        # Reparse the canonical payload instead of trusting a cached context value.
        base_enrichment = editorial_enrichment_from_json(base_payload)
        extraction = await self._enrichment._load_extraction(run, snapshot, extraction_artifact)
        projection = (
            await self._enrichment._load_projection(run, snapshot, extraction, projection_artifact)
            if projection_artifact is not None
            else None
        )
        synthesis = await self._enrichment._load_synthesis(
            run, snapshot, synthesis_artifact, extraction
        )
        fence = await self._validate_input_fence(
            snapshot=snapshot,
            base_artifact=base_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            extraction=extraction,
            projection=projection,
            synthesis=synthesis,
            read_only_media=True,
            read_only_snapshot=read_only_snapshot,
        )

        async with self._enrichment._uow_factory() as uow:
            artifacts = tuple(await uow.production_artifacts.list_for_run(run.id))
        by_id = {item.id: item for item in artifacts}

        def parent_id(artifact: ProductionArtifact) -> UUID | None:
            revision = artifact.metadata.get("revision")
            rebased = artifact.metadata.get("revision_rebase")
            value = (
                revision.get("base_artifact_id")
                if isinstance(revision, dict)
                else rebased.get("from_artifact_id")
                if isinstance(rebased, dict)
                else None
            )
            try:
                return UUID(str(value)) if value is not None else None
            except (TypeError, ValueError):
                return None

        chain: list[ProductionArtifact] = []
        seen: set[UUID] = set()
        cursor: ProductionArtifact | None = base_artifact
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            chain.append(cursor)
            ancestor_id = parent_id(cursor)
            cursor = by_id.get(ancestor_id) if ancestor_id is not None else None

        def delete_target(artifact: ProductionArtifact) -> tuple[str, str] | None:
            revision = artifact.metadata.get("revision")
            if not isinstance(revision, dict):
                return None
            operation = str(revision.get("operation", ""))
            kind_by_operation = {
                "delete_table": "table",
                "delete_diagram": "diagram",
                "delete_chart": "chart",
                "delete_source_figure": "source_figure",
            }
            kind = kind_by_operation.get(operation)
            target = revision.get("target")
            if kind is None and revision.get("action") == "remove_element":
                raw_kind = revision.get("element_kind")
                kind = "source_figure" if raw_kind == "figure" else str(raw_kind or "")
            key = target.get("element_key") if isinstance(target, dict) else None
            if key is None:
                key = revision.get("element_key")
            if (kind is None or not isinstance(key, str)) and isinstance(
                revision.get("target_path"), str
            ):
                import re

                matched = re.fullmatch(
                    r"editorial_enrichment\.(table|diagram|chart|figure|source_figure)\[([^\]]+)\]",
                    str(revision["target_path"]),
                )
                if matched is not None:
                    raw_kind, key = matched.groups()
                    kind = "source_figure" if raw_kind == "figure" else raw_kind
            if kind and isinstance(key, str):
                return kind, key
            return None

        deleted_after_source: list[tuple[int, tuple[str, str]]] = [
            (index, target)
            for index, artifact in enumerate(chain)
            if (target := delete_target(artifact)) is not None
        ]
        synthesis_by_hash: dict[str, ProductionSynthesisV1] = {}
        for artifact in artifacts:
            if (
                artifact.stage is not ProductionArtifactStage.SYNTHESIS
                or artifact.status is not ProductionArtifactStatus.VERIFIED
                or artifact.canonical_blob_id is None
            ):
                continue
            try:
                source_synthesis = production_synthesis_from_json(
                    await self._enrichment._artifact_store.read_json(artifact.canonical_blob_id)
                )
            except (ValueError, TypeError, KeyError):
                continue
            synthesis_by_hash[canonical_synthesis_hash(source_synthesis)] = source_synthesis

        source_enrichments: list[tuple[int, ProductionArtifact, EditorialEnrichmentV1]] = []
        for index, artifact in enumerate(chain):
            if (
                index == 0
                or artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT
                or artifact.status is not ProductionArtifactStatus.VERIFIED
                or artifact.canonical_blob_id is None
            ):
                continue
            try:
                source_payload = await self._enrichment._artifact_store.read_json(
                    artifact.canonical_blob_id
                )
                source_enrichment = editorial_enrichment_from_json(source_payload)
            except (ValueError, TypeError, KeyError):
                continue
            source_enrichments.append((index, artifact, source_enrichment))

        list_for_kind = {
            "table": "tables",
            "diagram": "diagrams",
            "chart": "charts",
            "source_figure": "source_figures",
        }
        current_keys = {
            (kind, item.key)
            for kind, attribute in list_for_kind.items()
            for item in getattr(base_enrichment, attribute)
        }
        block_candidates: dict[
            tuple[str, str],
            list[tuple[int, ProductionArtifact, Any, EditorialEnrichmentV1]],
        ] = {}
        annotation_candidates: dict[
            tuple[str, str, str], list[tuple[int, ProductionArtifact, Any, EditorialEnrichmentV1]]
        ] = {}
        for source_index, source_artifact, source_enrichment in source_enrichments:
            for kind, attribute in list_for_kind.items():
                for item in getattr(source_enrichment, attribute):
                    block_identity = (kind, item.key)
                    if block_identity not in current_keys:
                        block_candidates.setdefault(block_identity, []).append(
                            (source_index, source_artifact, item, source_enrichment)
                        )
            for annotation in source_enrichment.annotations:
                annotation_identity = (
                    annotation.paragraph_anchor,
                    annotation.role.value,
                    annotation.text,
                )
                if annotation not in base_enrichment.annotations:
                    annotation_candidates.setdefault(annotation_identity, []).append(
                        (source_index, source_artifact, annotation, source_enrichment)
                    )

        current_section_payloads = production_synthesis_to_json(synthesis)["sections"]
        allowed_refs = set(fence.evidence_pack._handle_to_ref.values())

        def with_current_hashes(value: EditorialEnrichmentV1) -> EditorialEnrichmentV1:
            return replace(
                value,
                extraction_hash=canonical_extraction_hash(extraction),
                synthesis_hash=canonical_synthesis_hash(synthesis),
            )

        def translate_item(
            item: Any, source_enrichment: EditorialEnrichmentV1
        ) -> tuple[Any | None, str | None]:
            placement = item.placement
            if placement.kind is not EnrichmentPlacementKind.AFTER_SECTION:
                return item, None
            source_synthesis = synthesis_by_hash.get(source_enrichment.synthesis_hash)
            if source_synthesis is None:
                return None, "source_synthesis_missing"
            source_sections = production_synthesis_to_json(source_synthesis)["sections"]
            source_index = placement.section_index
            if source_index is None or source_index >= len(source_sections):
                return None, "anchor_missing"
            source_identity = _enrichment_section_anchor_id(source_sections[source_index])
            matches = [
                index
                for index, section in enumerate(current_section_payloads)
                if _enrichment_section_anchor_id(section) == source_identity
            ]
            if len(matches) != 1:
                return None, "section_anchor_changed" if not matches else "section_anchor_ambiguous"
            if matches[0] == source_index:
                return item, None
            return replace(item, placement=replace(placement, section_index=matches[0])), None

        def validate_candidate(value: EditorialEnrichmentV1, *, kind: str, key: str) -> str | None:
            value = with_current_hashes(value)
            try:
                validate_editorial_enrichment(value, extraction=extraction, synthesis=synthesis)
            except (ValueError, TypeError, KeyError) as exc:
                return str(exc) or "editorial_enrichment_validation_failed"
            if not editorial_enrichment_evidence_refs(value) <= allowed_refs:
                return "evidence_not_in_current_pack"
            try:
                quality_rejections = revalidate_editorial_enrichment_quality(
                    value,
                    extraction=extraction,
                    synthesis=synthesis,
                    projection=projection,
                    expected_projection_hash=(
                        projection.projection_hash if projection is not None else None
                    ),
                )
            except (ValueError, TypeError, KeyError) as exc:
                return str(exc) or "editorial_enrichment_quality_validation_failed"
            candidate_rejection = next(
                (
                    rejection.reason_code
                    for rejection in quality_rejections
                    if rejection.block_id == key
                    and rejection.kind.casefold()
                    in {kind, "figure" if kind == "source_figure" else kind}
                ),
                None,
            )
            return candidate_rejection

        restored = base_enrichment
        restored_blocks: list[dict[str, Any]] = []
        non_restorable: list[dict[str, Any]] = []
        for (kind, key), candidates in sorted(block_candidates.items()):
            latest_reason = "block_validation_failed"
            latest_source = candidates[0][1]
            accepted = False
            for source_index, source_artifact, original_item, source_enrichment in candidates:
                latest_source = source_artifact
                if any(
                    deleted_target == (kind, key) and delete_index < source_index
                    for delete_index, deleted_target in deleted_after_source
                ):
                    latest_reason = "deleted_by_targeted_revision"
                    continue
                item, anchor_error = translate_item(original_item, source_enrichment)
                if item is None:
                    latest_reason = anchor_error or "anchor_invalid"
                    continue
                attribute = list_for_kind[kind]
                values = tuple((*getattr(restored, attribute), item))
                updates: dict[str, Any] = {attribute: values}
                if kind == "source_figure" and item.resolved_figure is not None:
                    figure_id = item.resolved_figure.figure_id
                    decisions = [
                        *restored.figure_decisions,
                        *(
                            decision
                            for decision in source_enrichment.figure_decisions
                            if decision.figure_id == figure_id
                            and decision not in restored.figure_decisions
                        ),
                    ]
                    updates["figure_decisions"] = tuple(decisions)
                candidate_enrichment = with_current_hashes(replace(restored, **updates))
                latest_reason = validate_candidate(candidate_enrichment, kind=kind, key=key) or ""
                if latest_reason:
                    continue
                if kind == "source_figure" and item.resolved_figure is not None:
                    catalog_entry = next(
                        (
                            entry
                            for entry in fence.figure_catalog
                            if entry.figure.figure_id == item.resolved_figure.figure_id
                        ),
                        None,
                    )
                    if (
                        catalog_entry is None
                        or catalog_entry.figure.sha256 != item.resolved_figure.sha256
                        or catalog_entry.figure.blob_id != item.resolved_figure.blob_id
                        or catalog_entry.figure.source_document_id
                        != item.resolved_figure.source_document_id
                        or catalog_entry.figure.locator != item.resolved_figure.locator
                    ):
                        latest_reason = "source_figure_not_in_current_inventory"
                        continue
                restored = candidate_enrichment
                restored_blocks.append(
                    {
                        "kind": kind,
                        "key": key,
                        "source_artifact_id": str(source_artifact.id),
                    }
                )
                accepted = True
                break
            if not accepted:
                non_restorable.append(
                    {
                        "kind": kind,
                        "key": key,
                        "source_artifact_id": str(latest_source.id),
                        "reason": latest_reason,
                    }
                )

        restored_annotation_ids = {
            (item.paragraph_anchor, item.role.value, item.text) for item in restored.annotations
        }
        for annotation_identity, candidates in sorted(annotation_candidates.items()):
            if annotation_identity in restored_annotation_ids:
                continue
            latest_reason = "annotation_validation_failed"
            latest_source = candidates[0][1]
            accepted = False
            for _source_index, source_artifact, annotation, _source_enrichment in candidates:
                latest_source = source_artifact
                value = with_current_hashes(
                    replace(restored, annotations=(*restored.annotations, annotation))
                )
                latest_reason = (
                    validate_candidate(value, kind="annotation", key=annotation.paragraph_anchor)
                    or ""
                )
                if latest_reason:
                    continue
                restored = value
                restored_blocks.append(
                    {
                        "kind": "annotation",
                        "key": annotation.paragraph_anchor,
                        "text": annotation.text,
                        "source_artifact_id": str(source_artifact.id),
                    }
                )
                restored_annotation_ids.add(annotation_identity)
                accepted = True
                break
            if not accepted:
                non_restorable.append(
                    {
                        "kind": "annotation",
                        "key": annotation_identity[0],
                        "text": annotation_identity[2],
                        "source_artifact_id": str(latest_source.id),
                        "reason": latest_reason,
                    }
                )
        if restored_blocks:
            restored = with_current_hashes(restored)
        return restored, restored_blocks, non_restorable

    @staticmethod
    def _targeted_request_identity_payload(
        request: TargetedEditorialRevisionRequest,
    ) -> dict[str, Any]:
        return enrichment_revision_rules.targeted_request_identity_payload(request)

    @classmethod
    def _targeted_request_identity(cls, request: TargetedEditorialRevisionRequest) -> str:
        return enrichment_revision_rules.targeted_request_identity(request)

    @staticmethod
    def _targeted_action(
        operation: TargetedEditorialRevisionOperation,
    ) -> EditorialEnrichmentRevisionAction:
        return enrichment_revision_rules.targeted_action(operation)

    @classmethod
    def _validate_targeted_request(cls, request: TargetedEditorialRevisionRequest) -> None:
        enrichment_revision_rules.validate_targeted_request(request)

    async def revise(
        self,
        *,
        subject_id: UUID,
        base_artifact_id: UUID,
        base_version: int,
        base_input_hash: str,
        base_canonical_sha256: str,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str | None,
        action: EditorialEnrichmentRevisionAction,
        instruction: str,
        targeted_request: TargetedEditorialRevisionRequest | None = None,
        actor: str | None = None,
    ) -> EditorialEnrichmentRevisionResult | TargetedSynthesisRevisionResult:
        normalized_instruction = instruction.strip()
        is_targeted_delete = targeted_request is not None and targeted_request.operation in {
            TargetedEditorialRevisionOperation.DELETE_TABLE,
            TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
            TargetedEditorialRevisionOperation.DELETE_CHART,
            TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
        }
        if (
            not normalized_instruction
            or len(normalized_instruction) > MAX_EDITORIAL_REVISION_INSTRUCTION_LENGTH
        ):
            raise ValueError("editorial_enrichment_revision_instruction_invalid")
        if targeted_request is not None:
            self._validate_targeted_request(targeted_request)
            if targeted_request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION:
                return TargetedSynthesisRevisionResult(
                    artifact=None,
                    publication_artifact=None,
                    revision=None,
                    refusal=TargetedEditorialRevisionRefusal(
                        code=TargetedEditorialRevisionRefusalCode.SOURCE_EXTENSION_NOT_AVAILABLE_YET,
                        recommended_restart_stage=None,
                        reason=(
                            "Source extension requires the E2a archival and extraction workflow."
                        ),
                        refusal_reason=(
                            TargetedEditorialRevisionRefusalReason.SOURCE_ADDITION_NOT_READY
                        ),
                        next_safe_action=(
                            "Complete source archival and extraction before using source extension."
                        ),
                    ),
                )
            request_identity_payload = self._targeted_request_identity_payload(targeted_request)
            request_identity = self._targeted_request_identity(targeted_request)
            revision_artifact_id = uuid5(
                NAMESPACE_URL, f"targeted-editorial-enrichment-revision:{request_identity}"
            )
        else:
            if element_key is None or not element_key.strip() or len(element_key) > 128:
                raise ValueError("editorial_enrichment_revision_element_key_invalid")
            request_identity_payload = {
                "subject_id": str(subject_id),
                "base_artifact_id": str(base_artifact_id),
                "base_version": base_version,
                "base_input_hash": base_input_hash,
                "base_canonical_sha256": base_canonical_sha256,
                "element_kind": element_kind.value,
                "element_key": element_key,
                "action": action.value,
                "instruction": normalized_instruction,
                "revision_prompt_version": EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
                "revision_contract_version": EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
            }
            request_identity = hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(request_identity_payload)
            ).hexdigest()
            revision_artifact_id = uuid5(
                NAMESPACE_URL, f"production-editorial-enrichment-revision:{request_identity}"
            )
        self._validate_action(element_kind, action)

        # A replay resolves the deterministic result before checking staleness:
        # after success, the base is intentionally no longer current.
        async with self._enrichment._uow_factory() as uow:
            prior_result = await uow.production_artifacts.get(revision_artifact_id)
            if prior_result is not None:
                if prior_result.subject_id != subject_id:
                    raise EditorialEnrichmentRevisionConflictError(
                        "Revision identity belongs to another subject"
                    )
                prior_revision = prior_result.metadata.get("revision")
                if not isinstance(prior_revision, dict):
                    raise ValueError("editorial_enrichment_revision_result_corrupt")
                publication = self._publication_for_revision(
                    await uow.production_artifacts.list_for_run(prior_result.production_run_id),
                    prior_result.id,
                )
                if targeted_request is not None:
                    return TargetedSynthesisRevisionResult(
                        artifact=prior_result,
                        publication_artifact=publication,
                        revision=prior_revision,
                    )
                return EditorialEnrichmentRevisionResult(
                    artifact=prior_result,
                    outcome=EditorialEnrichmentRevisionOutcome(prior_revision["outcome"]),
                    revision=prior_revision,
                    publication_artifact=publication,
                    previous_publication_artifact_id=self._optional_uuid(
                        prior_revision.get("previous_publication_artifact_id")
                    ),
                )

        context = await self._load_context(
            subject_id=subject_id,
            base_artifact_id=base_artifact_id,
            base_version=base_version,
            base_input_hash=base_input_hash,
            base_canonical_sha256=base_canonical_sha256,
        )
        (
            run,
            snapshot,
            base_artifact,
            base_payload,
            base_enrichment,
            references_artifact,
            extraction_artifact,
            projection_artifact,
            synthesis_artifact,
            previous_publication,
        ) = context

        extraction = await self._enrichment._load_extraction(run, snapshot, extraction_artifact)
        projection = (
            await self._enrichment._load_projection(run, snapshot, extraction, projection_artifact)
            if projection_artifact is not None
            else None
        )
        synthesis = await self._enrichment._load_synthesis(
            run, snapshot, synthesis_artifact, extraction
        )

        critical_source_waivers: tuple[dict[str, Any], ...] = ()
        if targeted_request is not None and not is_targeted_delete:
            references_blob_id = references_artifact.canonical_blob_id
            if references_blob_id is None:
                raise EditorialEnrichmentRevisionConflictError(
                    "The current References artifact has no canonical payload"
                )
            references_payload = await self._enrichment._artifact_store.read_json(
                references_blob_id
            )
            references = production_reference_corpus_from_json(references_payload)
            critical_failures, critical_source_waivers = await self._critical_source_assessment(
                context, extraction, references
            )
            if critical_failures:
                return self._targeted_refusal(
                    "A critical source has an extraction failure without a current, reasoned "
                    "analyst waiver; repair or waive the source first.",
                    ProductionStage.EXTRACTION,
                    refusal_reason=TargetedEditorialRevisionRefusalReason.SOURCE_EXTRACTION_FAILED,
                    next_safe_action=(
                        "Repair the failed source extraction or record a valid waiver, then retry."
                    ),
                )

        fence = await self._validate_input_fence(
            snapshot=snapshot,
            base_artifact=base_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            extraction=extraction,
            projection=projection,
            synthesis=synthesis,
        )
        access_policy = fence.access_policy
        inventory = fence.inventory
        figure_catalog = fence.figure_catalog
        evidence_pack = fence.evidence_pack
        evidence_pack_hash = fence.evidence_pack_hash
        inventory_hash = fence.inventory_hash
        input_hash = fence.input_hash
        if not access_policy.external_llm_allowed and not is_targeted_delete:
            if targeted_request is not None:
                return self._targeted_refusal(
                    "The current source access policy does not permit an editorial model call.",
                    None,
                    refusal_reason=(
                        TargetedEditorialRevisionRefusalReason.SOURCE_ACCESS_POLICY_BLOCKED
                    ),
                    next_safe_action=(
                        "Choose evidence that is permitted by the current source access policy."
                    ),
                )
            raise ValueError("editorial_enrichment_revision_policy_blocked")
        list_name = {
            EditorialEnrichmentElementKind.TABLE: "tables",
            EditorialEnrichmentElementKind.DIAGRAM: "diagrams",
            EditorialEnrichmentElementKind.CHART: "charts",
            EditorialEnrichmentElementKind.FIGURE: "source_figures",
        }[element_kind]
        base_elements = base_payload.get(list_name)
        if not isinstance(base_elements, list):
            raise ValueError("editorial_enrichment_revision_base_element_missing")
        is_create = targeted_request is not None and targeted_request.operation in {
            TargetedEditorialRevisionOperation.CREATE_TABLE,
            TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
            TargetedEditorialRevisionOperation.CREATE_CHART,
            TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
        }
        if is_create:
            element_key = self._generated_element_key(element_kind, request_identity, base_elements)
        assert element_key is not None
        element_index = next(
            (
                index
                for index, value in enumerate(base_elements)
                if isinstance(value, dict) and value.get("key") == element_key
            ),
            None,
        )
        if is_create and element_index is not None:
            raise EditorialEnrichmentRevisionConflictError(
                "The deterministic enrichment key is already in use"
            )
        if not is_create and element_index is None:
            raise ValueError("editorial_enrichment_revision_base_element_missing")
        base_element = base_elements[element_index] if element_index is not None else None
        element_evidence_handles = (
            self._element_evidence_handles(
                element_kind, base_enrichment, element_key, evidence_pack
            )
            if not is_create
            else []
        )
        admitted_handles = sorted(evidence_pack._handle_to_ref)
        is_delete = (
            targeted_request is not None
            and action is EditorialEnrichmentRevisionAction.REMOVE_ELEMENT
        )
        execution = None
        raw_text: str | None = None
        parse_identity: str | None = None
        validator_rejections: list[dict[str, str]] = []
        parsed: EditorialEnrichmentWireParseResult | None = None
        if not is_delete:
            editorial_contract: dict[str, Any] | None = None
            if is_create:
                base_editorial_request = build_editorial_enrichment_model_request(
                    run,
                    snapshot,
                    extraction,
                    synthesis,
                    evidence_pack,
                    access_policy,
                    source_figure_inventory_hash=inventory_hash,
                    figure_catalog=figure_catalog,
                    resource_search_enabled=self._enrichment._resource_search_enabled,
                )
                base_editorial_prompt = json.loads(base_editorial_request.text)
                editorial_contract = {
                    "prompt_version": EDITORIAL_ENRICHMENT_PROMPT_VERSION,
                    "contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
                    "instructions": base_editorial_prompt["instructions"],
                    "editorial_guidance": base_editorial_prompt["editorial_guidance"],
                    "output_contract": base_editorial_prompt["output_contract"],
                }
            placement_json = None
            if targeted_request is not None and targeted_request.target.placement is not None:
                if is_create:
                    try:
                        registered_placement = requested_registered_placement(
                            targeted_request=targeted_request,
                            synthesis=synthesis,
                            evidence_pack=evidence_pack,
                        )
                    except ValueError as exc:
                        return self._targeted_refusal(str(exc), None)
                    placement_json = {
                        "kind": registered_placement.kind.value,
                        "anchor_block_id": str(registered_placement.anchor_block_id),
                    }
                else:
                    placement_json = targeted_request.target.placement.to_json()
            request = self._model_request(
                request_identity=request_identity,
                request_identity_payload=request_identity_payload,
                action=action,
                instruction=normalized_instruction,
                element_kind=element_kind,
                element_key=element_key,
                base_element=base_element,
                element_evidence_handles=element_evidence_handles,
                evidence_pack=evidence_pack,
                evidence_pack_hash=evidence_pack_hash,
                figure_catalog=figure_catalog,
                access_policy=access_policy,
                create_mode=is_create,
                current_enrichment=base_payload if is_create else None,
                stable_synthesis_payload=(
                    production_synthesis_to_json(synthesis) if is_create else None
                ),
                target_placement=placement_json,
                editorial_contract=editorial_contract,
            )
            try:
                existing, archive_error = await self._enrichment._verified_existing_execution(
                    request
                )
                if archive_error is not None:
                    raise EditorialEnrichmentRevisionValidationError(
                        [
                            {
                                "block_id": "response",
                                "reason_code": str(archive_error.get("wire_error_code")),
                            }
                        ]
                    )
                execution = existing or await self._enrichment._model_gateway.draft(request)
            except (BridgeTransportError, ModelSubmissionReconciliationRequiredError) as exc:
                if request.run_id is None:
                    raise RuntimeError("targeted_revision_model_run_identity_missing") from exc
                transport_error = await _targeted_revision_transport_error(
                    self._enrichment._model_gateway,
                    exc,
                    request_identity=request_identity,
                    model_run_id=request.run_id,
                )
                raise transport_error from exc
            if execution.run.status.value != "succeeded":
                raise EditorialEnrichmentRevisionValidationError(
                    [{"block_id": "response", "reason_code": "editorial_enrichment_model_failed"}]
                )
            raw_text, raw_error = await self._enrichment._verified_raw_text(
                execution.run,
                expected_text=execution.output_text if existing is None else None,
            )
            if raw_error is not None or raw_text is None:
                raise EditorialEnrichmentRevisionValidationError(
                    [
                        {
                            "block_id": "response",
                            "reason_code": str((raw_error or {}).get("wire_error_code")),
                        }
                    ]
                )
            parsed = parse_editorial_enrichment_proposal_wire(
                raw_text, evidence_pack, figure_catalog=figure_catalog
            )
            parse_identity = await self._enrichment._record_wire_parse(
                execution.run, evidence_pack, parsed, figure_catalog=figure_catalog
            )
            validator_rejections = [
                {"block_id": item.block_id, "reason_code": item.reason_code}
                for item in parsed.rejections
            ]
            if parsed.error_code is not None:
                validator_rejections.append(
                    {"block_id": "proposal", "reason_code": parsed.error_code}
                )
            if targeted_request is not None and parsed.explicit_empty:
                reason = (
                    "The evidence does not establish a useful enrichment for this target."
                    if is_create
                    else (
                        "The proposed rewrite is not analytically useful; "
                        "delete the element instead."
                    )
                )
                return self._targeted_refusal(
                    reason,
                    None,
                    refusal_reason=TargetedEditorialRevisionRefusalReason.MODEL_DECLINED,
                    next_safe_action=(
                        "Reformulate the instruction or provide supporting evidence "
                        "via source extension."
                    ),
                )
            if parsed.proposal is None:
                if targeted_request is not None:
                    return self._targeted_refusal(
                        "; ".join(item["reason_code"] for item in validator_rejections)
                        or "The proposal is not supported by admitted evidence.",
                        None,
                        refusal_reason=(
                            TargetedEditorialRevisionRefusalReason.MODEL_OUTPUT_INVALID
                        ),
                        next_safe_action=(
                            "Reformulate the revision instruction or correct the proposed target."
                        ),
                    )
                raise EditorialEnrichmentRevisionValidationError(validator_rejections)
            if targeted_request is not None and parsed.proposal.resource_needs:
                return self._targeted_refusal(
                    "The requested enrichment needs evidence absent from the admitted pack.",
                    ProductionStage.REFERENCES,
                    refusal_reason=TargetedEditorialRevisionRefusalReason.SOURCE_EVIDENCE_MISSING,
                    next_safe_action=(
                        "Add supporting evidence through source extension, then preview again."
                    ),
                )
        else:
            assert targeted_request is not None

        is_remove = action is EditorialEnrichmentRevisionAction.REMOVE_ELEMENT
        resource_need_proposal = (
            parsed.proposal.resource_needs[0]
            if parsed is not None and parsed.proposal is not None and parsed.proposal.resource_needs
            else None
        )
        if (
            is_remove
            and not is_delete
            and resource_need_proposal is None
            and (parsed is None or not parsed.explicit_empty)
        ):
            raise EditorialEnrichmentRevisionValidationError(
                [
                    *validator_rejections,
                    {
                        "block_id": element_key,
                        "reason_code": "editorial_enrichment_revision_remove_requires_empty_marker",
                    },
                ]
            )
        validated_patch: EditorialEnrichmentV1 | None = None
        try:
            if (
                parsed is not None
                and parsed.proposal is not None
                and resource_need_proposal is None
                and not is_remove
            ):
                validated_patch = self._validate_target_proposal(
                    parsed,
                    element_kind=element_kind,
                    element_key=element_key,
                    evidence_pack=evidence_pack,
                    extraction=extraction,
                    synthesis=synthesis,
                    figure_catalog=figure_catalog,
                    validator_rejections=validator_rejections,
                )
                candidate_elements = {
                    EditorialEnrichmentElementKind.TABLE: validated_patch.tables,
                    EditorialEnrichmentElementKind.DIAGRAM: validated_patch.diagrams,
                    EditorialEnrichmentElementKind.CHART: validated_patch.charts,
                    EditorialEnrichmentElementKind.FIGURE: validated_patch.source_figures,
                }[element_kind]
                if len(candidate_elements) != 1:
                    reason_code = "editorial_enrichment_target_failed_final_validation"
                    try:
                        validate_editorial_enrichment_proposal(
                            parsed.proposal,
                            evidence_pack,
                            extraction,
                            synthesis,
                            figure_catalog=figure_catalog,
                            _drop_invalid_blocks=False,
                        )
                    except EditorialEnrichmentProposalControlError as exc:
                        code = getattr(exc, "code", None)
                        reason_code = (
                            str(getattr(code, "value", code)) if code is not None else reason_code
                        )
                        rejection = {"block_id": element_key, "reason_code": reason_code}
                        if exc.diagnostic_code:
                            rejection["diagnostic_code"] = exc.diagnostic_code
                        if str(exc):
                            rejection["diagnostic"] = str(exc)
                    else:
                        rejection = {"block_id": element_key, "reason_code": reason_code}
                    validator_rejections.append(rejection)
                    raise EditorialEnrichmentRevisionValidationError(validator_rejections)
            if base_artifact.canonical_blob_id is None:
                raise EditorialEnrichmentRevisionConflictError(
                    "The requested base artifact has no canonical payload"
                )
            artifact_store = self._enrichment._artifact_store
            if artifact_store is None:
                raise ValueError("editorial_enrichment_revision_inputs_missing")
            built_candidate = await build_candidate_from_validated_patch(
                base_artifact=base_artifact,
                base_payload=base_payload,
                base_canonical_bytes=await artifact_store.read_bytes(
                    base_artifact.canonical_blob_id
                ),
                base_canonical_sha256=base_canonical_sha256,
                base_enrichment=base_enrichment,
                validated_patch=validated_patch,
                extraction=extraction,
                synthesis=synthesis,
                element_kind=element_kind,
                element_key=element_key,
                action=action,
                input_hash=input_hash,
                artifact_id=revision_artifact_id,
                targeted_request=targeted_request,
                evidence_pack=evidence_pack,
                is_create=is_create,
                is_delete=is_delete,
                resource_need_proposal=resource_need_proposal,
            )
            await self._persistence.assert_editorial_enrichment_revision_fence(
                run_id=base_artifact.production_run_id,
                expected_current_artifact_id=base_artifact.id,
                expected_current_input_hash=base_artifact.input_hash,
                expected_current_canonical_sha256=base_canonical_sha256,
            )
            built_candidate = await materialize_candidate_diagrams(
                built_candidate,
                materializer=lambda diagram: self._materialize_candidate_diagram(
                    diagram, run_id=run.id
                ),
            )
        except EditorialEnrichmentRevisionValidationError as exc:
            if targeted_request is not None:
                reason = (
                    "; ".join(item["reason_code"] for item in exc.rejections)
                    or "The proposal failed an editorial enrichment rule."
                )
                if any(token in reason for token in ("trivial", "ioc_republication")):
                    reason = f"{reason}; delete the element instead of keeping a trivial rewrite."
                return self._targeted_refusal(
                    reason,
                    None,
                    refusal_reason=TargetedEditorialRevisionRefusalReason.MODEL_OUTPUT_INVALID,
                    next_safe_action=(
                        "Reformulate the instruction or correct the proposed enrichment block."
                    ),
                    details={"validator_rejections": exc.rejections},
                )
            raise
        except ValueError as exc:
            if targeted_request is not None:
                return self._targeted_refusal(
                    str(exc) or "The proposal failed canonical enrichment validation.",
                    None,
                    refusal_reason=TargetedEditorialRevisionRefusalReason.MODEL_OUTPUT_INVALID,
                    next_safe_action=(
                        "Reformulate the revision instruction or correct the proposed target."
                    ),
                )
            raise
        revised_enrichment = built_candidate.enrichment
        final_payload = built_candidate.canonical_payload
        outcome = built_candidate.outcome
        resource_need = built_candidate.resource_need
        after_element = built_candidate.after_element
        if not is_remove and outcome is EditorialEnrichmentRevisionOutcome.REVISED:
            element_evidence_handles = self._element_evidence_handles(
                element_kind, revised_enrichment, element_key, evidence_pack
            )
        before_publication_id = previous_publication.id if previous_publication else None
        revision_data: dict[str, Any] = {
            "request_identity": request_identity,
            "outcome": outcome.value,
            "element_kind": element_kind.value,
            "element_key": element_key,
            "action": action.value,
            "instruction": normalized_instruction,
            "base_artifact_id": str(base_artifact.id),
            "base_version": base_artifact.version,
            "base_input_hash": base_artifact.input_hash,
            "base_canonical_sha256": base_canonical_sha256,
            "element_evidence_handles": element_evidence_handles,
            "admitted_evidence_handles": admitted_handles,
            "element_before": base_element,
            "element_after": after_element,
            "validator_results": [
                {"validator": "l1b_text_block_parser", "status": "passed"},
                {"validator": "l7b_evidence_handles", "status": "passed"},
                {"validator": "l8_enrichment_grounding", "status": "passed"},
                {"validator": "canonical_enrichment", "status": "passed"},
            ],
            "criticality_gate_version": "critical-source-waiver-v1",
            "critical_source_waivers": [dict(item) for item in critical_source_waivers],
            "validator_rejections": validator_rejections,
            "parse_identity": parse_identity,
            "previous_publication_artifact_id": (
                str(before_publication_id) if before_publication_id else None
            ),
            "resource_need": resource_need,
        }
        if targeted_request is not None and is_delete:
            revision_data["validator_results"] = [
                {"validator": "target_scope", "status": "passed"},
                {"validator": "canonical_enrichment", "status": "passed"},
            ]
        revision_data["source_figure_provenance_version"] = (
            SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION
        )
        revision_data["source_figure_provenance_diagnostics"] = (
            build_source_figure_provenance_diagnostics(
                inventory, figure_catalog, revised_enrichment
            )
        )
        element_provenance: dict[str, Any] | None = None
        if targeted_request is not None:
            revision_id = str(revision_artifact_id)
            target_json = targeted_request.target.to_json()
            target_json["element_key"] = element_key
            before_bytes = ProductionArtifactStore.canonical_json_bytes({"value": base_element})
            after_bytes = ProductionArtifactStore.canonical_json_bytes({"value": after_element})
            evidence_refs = [
                {
                    "handle": handle,
                    "ref": {
                        "source_document_id": str(ref.source_document_id),
                        "kind": ref.kind.value,
                        "evidence_key": ref.evidence_key,
                    },
                }
                for handle in element_evidence_handles
                if (ref := evidence_pack._handle_to_ref.get(handle)) is not None
            ]
            revision_data.update(
                {
                    "revision_id": revision_id,
                    "base_artifact_hash": base_canonical_sha256,
                    "target": target_json,
                    "target_path": f"editorial_enrichment.{element_kind.value}[{element_key}]",
                    "operation": targeted_request.operation.value,
                    "request": normalized_instruction,
                    "mode": targeted_request.mode.value,
                    "actor": targeted_request.requester,
                    "model_run_id": str(execution.run.id) if execution is not None else None,
                    "prompt_version": EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
                    "contract_version": EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
                    "editorial_contract_prompt_version": EDITORIAL_ENRICHMENT_PROMPT_VERSION,
                    "editorial_contract_version": EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
                    "parser_version": EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
                    "validator_version": "editorial-enrichment-validator-v2-source-waiver",
                    "evidence_refs": evidence_refs,
                    "evidence_handles": element_evidence_handles,
                    "source_additions": [],
                    "before_hash": hashlib.sha256(before_bytes).hexdigest(),
                    "after_hash": hashlib.sha256(after_bytes).hexdigest(),
                    "rationale": (
                        "Target deleted without a model call."
                        if is_delete
                        else "Element validated against the admitted enrichment evidence pack."
                    ),
                    "created_at": datetime.now(UTC).isoformat(),
                    "result_artifact_id": str(revision_artifact_id),
                }
            )
            element_provenance = await self._element_provenance_after_revision(
                base_artifact=base_artifact,
                base_payload=base_payload,
                revised_payload=final_payload,
                element_kind=element_kind,
                element_key=element_key,
                revision_id=revision_id,
                result_artifact_id=revision_artifact_id,
                is_delete=is_delete,
            )
        metadata_extra = {
            "source_figure_provenance_version": SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION,
            "source_figure_provenance_diagnostics": build_source_figure_provenance_diagnostics(
                inventory, figure_catalog, revised_enrichment
            ),
            "revision": revision_data,
            **(
                {"element_provenance": element_provenance} if element_provenance is not None else {}
            ),
        }
        artifact_store = self._enrichment._artifact_store
        if artifact_store is None:
            raise ValueError("editorial_enrichment_revision_inputs_missing")
        async with self._enrichment._uow_factory() as uow:
            run_guard = None
            base_workspace_snapshot = None
            revision_actor_id = None
            if self._revision_effects is not None:
                actor_value = actor or (
                    targeted_request.requester if targeted_request is not None else None
                )
                if actor_value is None:
                    raise EditorialEnrichmentRevisionConflictError(
                        "editorial_revision_actor_invalid"
                    )
                if not actor_value.strip():
                    raise EditorialEnrichmentRevisionConflictError(
                        "editorial_revision_actor_invalid"
                    )
                revision_actor_id = actor_value
                try:
                    run_guard = await self._revision_effects.acquire_run_guard(uow, run.id)
                    await self._revision_effects.lock_run_row(uow, run_guard)
                    current = await uow.production_artifacts.get_current(
                        run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
                    )
                    if current is None or current.id != base_artifact.id:
                        raise EditorialEnrichmentRevisionConflictError(
                            "The requested base artifact is stale"
                        )
                    base_workspace_snapshot = await self._revision_effects.current_snapshot_in_uow(
                        uow, subject_id
                    )
                    if base_workspace_snapshot.production_run_id != run.id:
                        raise EditorialEnrichmentRevisionConflictError(
                            "The requested production run is no longer current"
                        )
                except EditorialRevisionEffectsError as error:
                    raise EditorialEnrichmentRevisionConflictError(error.code) from None
            artifact = await persist_candidate(
                uow,
                artifact_store=artifact_store,
                persistence_service=self._persistence,
                candidate=built_candidate,
                raw_result=raw_text,
                model_run_id=execution.run.id if execution is not None else None,
                evidence_pack_hash=evidence_pack_hash,
                access_policy_hash=str(base_artifact.metadata["access_policy_hash"]),
                model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
                routing_policy_version=str(
                    base_artifact.metadata.get("routing_policy_version", "")
                ),
                projection_hash=projection.projection_hash if projection is not None else None,
                source_figure_inventory_hash=inventory_hash,
                metadata_extra=metadata_extra,
            )
            if self._revision_effects is not None:
                assert run_guard is not None
                assert base_workspace_snapshot is not None
                assert revision_actor_id is not None
                try:
                    await self._revision_effects.apply_after_write(
                        uow,
                        run_guard=run_guard,
                        base_snapshot=base_workspace_snapshot,
                        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                        artifact=artifact,
                        canonical_sha256=built_candidate.canonical_sha256,
                        canonical_content=final_payload,
                        lineage=None,
                        actor_id=revision_actor_id,
                    )
                except EditorialRevisionEffectsError as error:
                    raise EditorialEnrichmentRevisionConflictError(error.code) from None
            await uow.commit()
        # Publication assembly and preview rendering remain post-commit behind
        # their existing services; they are not part of the candidate UOW.
        publication = await self._assemble_and_render(
            run=run,
            snapshot=snapshot,
            references_artifact=references_artifact,
            extraction_artifact=extraction_artifact,
            projection_artifact=projection_artifact,
            synthesis_artifact=synthesis_artifact,
            enrichment_artifact=artifact,
            references=production_reference_corpus_from_json(
                await self._enrichment._artifact_store.read_json(
                    references_artifact.canonical_blob_id
                )
            ),
            extraction=extraction,
            projection=projection,
            synthesis=synthesis,
            enrichment=revised_enrichment,
        )
        if targeted_request is not None:
            return TargetedSynthesisRevisionResult(
                artifact=artifact,
                publication_artifact=publication,
                revision=revision_data,
            )
        return EditorialEnrichmentRevisionResult(
            artifact=artifact,
            outcome=outcome,
            revision=revision_data,
            publication_artifact=publication,
            previous_publication_artifact_id=before_publication_id,
        )

    async def _load_context(
        self,
        *,
        subject_id: UUID,
        base_artifact_id: UUID,
        base_version: int,
        base_input_hash: str,
        base_canonical_sha256: str,
    ) -> tuple[Any, ...]:
        from cti_app.domain.production import ProductionArtifactStage

        async with self._enrichment._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(subject_id)
            base = await uow.production_artifacts.get(base_artifact_id)
            if run is None or base is None or base.production_run_id != run.id:
                raise EditorialEnrichmentRevisionConflictError(
                    "The requested base artifact is no longer current"
                )
            current = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
            )
            if (
                current is None
                or current.id != base.id
                or base.version != base_version
                or base.input_hash != base_input_hash
                or base.status is not ProductionArtifactStatus.VERIFIED
                or base.canonical_blob_id is None
            ):
                raise EditorialEnrichmentRevisionConflictError(
                    "The requested base artifact is stale"
                )
            snapshot = await uow.production_input_snapshots.get_by_run(run.id)
            references = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.REFERENCES.value
            )
            extraction = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EXTRACTION.value
            )
            projection = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
            )
            synthesis = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.SYNTHESIS.value
            )
            publication = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.PUBLICATION.value
            )
        if snapshot is None or references is None or extraction is None or synthesis is None:
            raise ValueError("editorial_enrichment_revision_inputs_missing")
        for stage_artifact in (references, extraction, synthesis):
            if (
                stage_artifact.status is not ProductionArtifactStatus.VERIFIED
                or stage_artifact.canonical_blob_id is None
            ):
                raise ValueError("editorial_enrichment_revision_inputs_missing")
        base_bytes = await self._enrichment._artifact_store.read_bytes(base.canonical_blob_id)
        if hashlib.sha256(base_bytes).hexdigest() != base_canonical_sha256:
            raise EditorialEnrichmentRevisionConflictError(
                "The requested base artifact content hash changed"
            )
        base_payload = json.loads(base_bytes)
        if not isinstance(base_payload, dict):
            raise ValueError("editorial_enrichment_revision_base_invalid")
        base_enrichment = editorial_enrichment_from_json(base_payload)
        return (
            run,
            snapshot,
            base,
            base_payload,
            base_enrichment,
            references,
            extraction,
            projection,
            synthesis,
            publication,
        )

    @staticmethod
    def _validate_action(
        element_kind: EditorialEnrichmentElementKind,
        action: EditorialEnrichmentRevisionAction,
    ) -> None:
        enrichment_revision_rules.validate_targeted_action(element_kind, action)

    @staticmethod
    def _element_evidence_handles(
        element_kind: EditorialEnrichmentElementKind,
        enrichment: EditorialEnrichmentV1,
        element_key: str,
        evidence_pack: EditorialEnrichmentEvidencePackV1,
    ) -> list[str]:
        refs: set[ExtractionEvidenceRefV1] = set()
        if element_kind is EditorialEnrichmentElementKind.TABLE:
            table_element = next(item for item in enrichment.tables if item.key == element_key)
            refs.update(ref for row in table_element.rows for ref in row.evidence_refs)
            if table_element.purpose is not None:
                refs.update(table_element.purpose.evidence_refs)
        elif element_kind is EditorialEnrichmentElementKind.DIAGRAM:
            diagram_element = next(item for item in enrichment.diagrams if item.key == element_key)
            refs.update(ref for node in diagram_element.nodes for ref in node.evidence_refs)
            refs.update(ref for edge in diagram_element.edges for ref in edge.evidence_refs)
            if diagram_element.purpose is not None:
                refs.update(diagram_element.purpose.evidence_refs)
        elif element_kind is EditorialEnrichmentElementKind.CHART:
            chart_element = next(item for item in enrichment.charts if item.key == element_key)
            refs.update(ref for point in chart_element.points for ref in point.evidence_refs)
            if chart_element.purpose is not None:
                refs.update(chart_element.purpose.evidence_refs)
        else:
            figure_element = next(
                item for item in enrichment.source_figures if item.key == element_key
            )
            if figure_element.resolved_figure is not None:
                decision = next(
                    (
                        item
                        for item in enrichment.figure_decisions
                        if item.figure_id == figure_element.resolved_figure.figure_id
                    ),
                    None,
                )
                if decision is not None:
                    refs.update(decision.evidence_refs)
        handles_by_ref = {ref: handle for handle, ref in evidence_pack._handle_to_ref.items()}
        if not refs <= handles_by_ref.keys():
            raise EditorialEnrichmentRevisionConflictError(
                "The base element cites evidence outside its admitted evidence pack"
            )
        handles = sorted(handles_by_ref[ref] for ref in refs)
        if element_kind is EditorialEnrichmentElementKind.FIGURE:
            source_figure = next(
                item for item in enrichment.source_figures if item.key == element_key
            )
            if source_figure.resolved_figure is not None:
                decision = next(
                    (
                        item
                        for item in enrichment.figure_decisions
                        if item.figure_id == source_figure.resolved_figure.figure_id
                    ),
                    None,
                )
                if decision is not None:
                    handles.append(decision.handle)
        return sorted(set(handles))

    @staticmethod
    def _model_request(
        *,
        request_identity: str,
        request_identity_payload: dict[str, Any],
        action: EditorialEnrichmentRevisionAction,
        instruction: str,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        base_element: dict[str, Any] | None,
        element_evidence_handles: list[str],
        evidence_pack: EditorialEnrichmentEvidencePackV1,
        evidence_pack_hash: str,
        figure_catalog: tuple[Any, ...],
        access_policy: Any,
        create_mode: bool = False,
        current_enrichment: dict[str, Any] | None = None,
        stable_synthesis_payload: dict[str, Any] | None = None,
        target_placement: dict[str, Any] | None = None,
        editorial_contract: dict[str, Any] | None = None,
    ) -> Any:
        from cti_app.application.model_gateway import ModelRequest, ModelRoutingHint

        target_base_element = base_element
        if element_kind is EditorialEnrichmentElementKind.FIGURE and base_element is not None:
            resolved = base_element.get("resolved_figure")
            resolved_id = resolved.get("figure_id") if isinstance(resolved, dict) else None
            catalog_record: dict[str, Any] = next(
                (
                    entry.prompt_record(
                        evidence_handles=evidence_pack.evidence_handles_for_source(
                            entry.figure.source_document_id
                        )
                    )
                    for entry in figure_catalog
                    if str(entry.figure.figure_id) == resolved_id
                ),
                {},
            )
            from cti_app.application.production_editorial_enrichment import (
                _safe_figure_prompt_text,
            )

            target_base_element = {
                "key": element_key,
                "caption": _safe_figure_prompt_text(base_element.get("caption")),
                "figure_handle": catalog_record.get("handle"),
                "anchor": catalog_record.get("anchor")
                or catalog_record.get("figure_label")
                or catalog_record.get("nearby_heading"),
                "page": catalog_record.get("page"),
                "dimensions": catalog_record.get("dimensions", {"width": None, "height": None}),
                "provenance_summary": catalog_record.get(
                    "provenance_summary", "Archived source media"
                ),
            }

        prompt_payload = {
            "instructions": (
                (
                    "Crée uniquement l'élément ciblé avec la clé fournie; n'ajoute ni ne réécris "
                    "aucun autre élément. Le pipeline demandé et chaque relation doivent être "
                    "établis par les preuves admises. Si ce n'est pas démontré, retourne "
                    "uniquement NO USEFUL ENRICHMENT. Respecte le placement demandé et le "
                    "contrat EDITORIAL_ENRICHMENT fourni. "
                    if create_mode
                    else "Révise uniquement l'élément ciblé. "
                )
                + "Utilise exclusivement les handles du "
                "paquet de preuves admis. Conserve les littéraux exactement. Ne fais aucune "
                "recherche, aucun appel web, n'invente aucun fait et ne modifie pas la "
                "narration. Si l'instruction exige une information absente des preuves, "
                "émets uniquement un bloc NEEDS avec un motif et un query_hint, sans proposer "
                "l'élément révisé. Pour l'action remove_element, retourne uniquement le marqueur "
                "exact NO USEFUL ENRICHMENT afin de supprimer l'élément ciblé; sinon retourne "
                "un seul bloc du type ciblé (TABLE, DIAGRAM, CHART ou FIGURE), "
                "en conservant sa clé "
                "pour TABLE/DIAGRAM/CHART. Respecte strictement "
                "les handles et règles de validation fournis. Pour une FIGURE, sélectionne "
                "uniquement un FIGURE_HANDLE accepté du catalogue, avec des evidence handles "
                "de la même source. Une architecture, chaîne d'exploitation ou chaîne malware, "
                "infrastructure, capture réseau, capture de code informative, screenshot de "
                "leurre, visualisation d'analyse ou graphique source peut être utile. Rejette "
                "logo, header/footer, bannière, hero décoratif, illustration marketing, portrait "
                "sans valeur analytique, image d'article adjacent ou figure sans lien direct aux "
                "preuves. Une URL ou une image ne remplace jamais le handle. La caption doit "
                "être courte et descriptive; si elle manque, le code préfère la caption source, "
                "puis un alt exploitable, et refuse de publier sans l'un de ces textes. Une table "
                "ne doit pas republier les IP, domaines, URL, hachés ou e-mails projetés lorsque "
                "ses autres colonnes sont constantes ou répètent seulement leur type; conserve-la "
                "si elle relie les IOC à des attributs analytiques utiles et documentés. Un "
                "diagramme doit clarifier une séquence, un pipeline, une architecture, des "
                "branches, une convergence, plusieurs relations, un contrôle-flow, une propagation "
                "ou un mapping complexe. Une branche ou convergence à un seul niveau autour d'un "
                "seul nœud ne suffit pas si au moins 80% des libellés réutilisent au moins la "
                "moitié "
                "de leurs termes de contenu d'une phrase d'ancrage, sans ajouter de chemin multi-"
                "étapes ou de mapping distinct; omets alors le bloc. "
                "Aucun nombre minimal de nœuds ne s'applique. Dans GAIN, réponds obligatoirement à "
                "« Qu'est-ce que le lecteur comprend mieux grâce à cet élément ? » par un gain "
                "concret; sinon omets le bloc."
            ),
            "action": action.value,
            "instruction": instruction,
            "target": {
                "kind": element_kind.value,
                "key": element_key,
                "base_element": target_base_element,
                "base_element_evidence_handles": element_evidence_handles,
                "placement": target_placement,
            },
            "current_editorial_enrichment": current_enrichment,
            "editorial_contract": editorial_contract,
            "stable_current_synthesis": (
                ProductionEditorialEnrichmentRevisionService._render_stable_synthesis(
                    stable_synthesis_payload
                )
                if stable_synthesis_payload is not None
                else None
            ),
            "requested_operation": request_identity_payload.get("operation", action.value),
            "admitted_evidence_pack": {
                "schema_version": 4,
                "policy_version": evidence_pack.policy_version,
                "relevance_projection_hash": evidence_pack.projection_hash,
                "publication_language": evidence_pack.publication_language,
                "current_synthesis": dict(evidence_pack.current_synthesis),
                "narrative_evidence": [dict(value) for value in evidence_pack.narrative_evidence],
                "technical_evidence": [dict(value) for value in evidence_pack.technical_evidence],
                "reserve_evidence": [dict(value) for value in evidence_pack.reserve_evidence],
                "source_pair_relations": [
                    dict(value) for value in evidence_pack.source_pair_relations
                ],
            },
            "figure_catalog": [
                value.prompt_record(
                    evidence_handles=evidence_pack.evidence_handles_for_source(
                        value.figure.source_document_id
                    )
                )
                for value in figure_catalog
            ],
            "output_contract": editorial_enrichment_output_contract_example(),
            "output_contract_version": (EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION),
        }
        return ModelRequest(
            text=json.dumps(
                prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            prompt_template_id="production-editorial-enrichment",
            prompt_template_version=EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
            evidence_pack_hash=evidence_pack_hash,
            external_llm_allowed=access_policy.external_llm_allowed,
            routing_hint=ModelRoutingHint.EDITORIAL_ENRICHMENT,
            sensitivity=access_policy.effective_tlp.value,
            web_search=False,
            background=False,
            conversation=None,
            run_id=uuid5(
                NAMESPACE_URL, f"production-editorial-enrichment-revision-model:{request_identity}"
            ),
            allow_failed_resubmit=True,
            metadata={
                "editorial_enrichment_input_hash": request_identity_payload["base_input_hash"],
                "editorial_enrichment_invocation_hash": request_identity,
                "editorial_revision_request_identity": request_identity,
                "editorial_revision_action": action.value,
                "editorial_revision_element_key": element_key,
                "evidence_pack_hash": evidence_pack_hash,
                "model_policy_version": EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
                "revision_request": request_identity_payload,
            },
            parameters={
                "contract_version": EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
                "revision": True,
            },
        )

    def _validate_target_proposal(
        self,
        parsed: EditorialEnrichmentWireParseResult,
        *,
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        evidence_pack: EditorialEnrichmentEvidencePackV1,
        extraction: Any,
        synthesis: Any,
        figure_catalog: tuple[Any, ...],
        validator_rejections: list[dict[str, str]],
    ) -> EditorialEnrichmentV1:
        return enrichment_revision_rules.validate_editorial_enrichment_patch(
            parsed,
            element_kind=element_kind,
            element_key=element_key,
            evidence_pack=evidence_pack,
            extraction=extraction,
            synthesis=synthesis,
            figure_catalog=figure_catalog,
            validator_rejections=validator_rejections,
        )

    async def _materialize_candidate_diagram(self, diagram: Any, *, run_id: UUID) -> Any:
        """Compile and store a diagram during the explicit candidate materialization step."""
        if (
            self._enrichment._diagram_compiler is None
            or self._enrichment._media_asset_store is None
        ):
            raise ValueError("editorial_enrichment_diagram_compilation_unavailable")
        compilation = await compile_and_store_diagrams(
            (diagram,),
            compiler=self._enrichment._diagram_compiler,
            media_asset_store=self._enrichment._media_asset_store,
            production_run_id=run_id,
        )
        if compilation.rejections:
            rejection = compilation.rejections[0]
            raise EditorialEnrichmentRevisionValidationError(
                [
                    {
                        "block_id": rejection.diagram_key,
                        "reason_code": rejection.warning_code,
                        "compiler_error_code": rejection.reason_code,
                    }
                ]
            )
        return compilation.diagrams[0]

    @staticmethod
    def _generated_element_key(
        element_kind: EditorialEnrichmentElementKind,
        request_identity: str,
        existing_elements: list[Any],
    ) -> str:
        existing = {value.get("key") for value in existing_elements if isinstance(value, dict)}
        prefix = element_kind.value + "_"
        digest_length = 64 - len(prefix)
        counter = 0
        while True:
            seed = request_identity if counter == 0 else f"{request_identity}:{counter}"
            key = f"{prefix}{hashlib.sha256(seed.encode()).hexdigest()[:digest_length]}"
            if key not in existing:
                return key
            counter += 1

    @staticmethod
    def _render_stable_synthesis(payload: dict[str, Any]) -> str:
        from cti_app.application.production_synthesis_revision import _render_current_synthesis

        return _render_current_synthesis(payload)

    @staticmethod
    def _assert_requested_placement(
        *,
        candidate: EditorialEnrichmentV1,
        element_kind: EditorialEnrichmentElementKind,
        targeted_request: TargetedEditorialRevisionRequest | None,
        synthesis: Any,
        evidence_pack: Any | None = None,
    ) -> None:
        enrichment_revision_rules.assert_requested_placement(
            candidate=candidate,
            element_kind=element_kind,
            targeted_request=targeted_request,
            synthesis=synthesis,
            evidence_pack=evidence_pack,
        )

    @staticmethod
    def _targeted_refusal(
        reason: str,
        restart_stage: ProductionStage | None,
        *,
        refusal_reason: TargetedEditorialRevisionRefusalReason = (
            TargetedEditorialRevisionRefusalReason.MODEL_OUTPUT_INVALID
        ),
        next_safe_action: str = "Reformulate the instruction or add supporting evidence.",
        details: Mapping[str, Any] | None = None,
    ) -> TargetedSynthesisRevisionResult:
        return TargetedSynthesisRevisionResult(
            artifact=None,
            publication_artifact=None,
            revision=None,
            refusal=TargetedEditorialRevisionRefusal(
                code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                recommended_restart_stage=restart_stage,
                reason=reason,
                refusal_reason=refusal_reason,
                next_safe_action=next_safe_action,
                details=dict(details or {}),
            ),
        )

    async def _element_provenance_after_revision(
        self,
        *,
        base_artifact: ProductionArtifact,
        base_payload: dict[str, Any],
        revised_payload: dict[str, Any],
        element_kind: EditorialEnrichmentElementKind,
        element_key: str,
        revision_id: str,
        result_artifact_id: UUID,
        is_delete: bool,
    ) -> dict[str, Any]:
        return await self._lineage.element_provenance_after_revision(
            base_artifact=base_artifact,
            base_payload=base_payload,
            revised_payload=revised_payload,
            element_kind=element_kind,
            element_key=element_key,
            revision_id=revision_id,
            result_artifact_id=result_artifact_id,
            is_delete=is_delete,
        )

    async def lineage_targeted(self, artifact_id: UUID) -> dict[str, Any]:
        return await self._lineage.lineage_targeted(artifact_id)

    async def _assemble_and_render(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        projection_artifact: ProductionArtifact | None,
        synthesis_artifact: ProductionArtifact,
        enrichment_artifact: ProductionArtifact,
        references: Any,
        extraction: Any,
        projection: Any,
        synthesis: Any,
        enrichment: EditorialEnrichmentV1,
    ) -> ProductionArtifact:
        from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
        from cti_app.application.publication_assembly import PublicationAssemblyService

        async with self._enrichment._uow_factory() as uow:
            routing_decision = await get_adopted_production_routing_decision(uow=uow, run=run)
            workspace_snapshot_record = await EditorialWorkspaceService(
                uow_factory=cast(Any, self._enrichment._uow_factory),
                artifact_store=self._enrichment._artifact_store,
            ).snapshot_record_in_uow(cast(Any, uow), snapshot.subject_id)
            assembly = await PublicationAssemblyService(
                self._enrichment._artifact_store, uow.production_artifacts
            ).assemble_publication(
                run=run,
                snapshot=snapshot,
                references=references,
                extraction=extraction,
                relevance_projection=projection,
                synthesis=synthesis,
                editorial_enrichment=enrichment,
                editorial_enrichment_artifact_id=enrichment_artifact.id,
                editorial_enrichment_artifact_input_hash=enrichment_artifact.input_hash,
                workspace_snapshot_record=workspace_snapshot_record,
                publication_kind=(
                    routing_decision.publication_kind if routing_decision is not None else None
                ),
                routing_decision_hash=(
                    routing_decision.decision_hash if routing_decision is not None else None
                ),
                routing_policy_version=(
                    routing_decision.policy_version if routing_decision is not None else None
                ),
                metadata_extra={
                    "input_artifacts": {
                        "references_artifact_id": str(references_artifact.id),
                        "extraction_artifact_id": str(extraction_artifact.id),
                        "relevance_projection_artifact_id": (
                            str(projection_artifact.id) if projection_artifact else None
                        ),
                        "synthesis_artifact_id": str(synthesis_artifact.id),
                        "editorial_enrichment_artifact_id": str(enrichment_artifact.id),
                    },
                    "editorial_revision_artifact_id": str(enrichment_artifact.id),
                },
            )
            await uow.commit()
        publication = assembly.artifact
        await self._publication_render_service.render_preview(publication.id)
        return publication

    @staticmethod
    def _publication_for_revision(
        artifacts: Sequence[ProductionArtifact],
        enrichment_artifact_id: UUID,
    ) -> ProductionArtifact | None:
        for item in artifacts:
            inputs = item.metadata.get("input_artifacts")
            if (
                item.stage is ProductionArtifactStage.PUBLICATION
                and isinstance(inputs, dict)
                and inputs.get("editorial_enrichment_artifact_id") == str(enrichment_artifact_id)
            ):
                return item
        return None

    @staticmethod
    def _optional_uuid(value: Any) -> UUID | None:
        if value is None:
            return None
        return UUID(str(value))

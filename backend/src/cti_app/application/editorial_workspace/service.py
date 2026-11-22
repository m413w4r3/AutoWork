"""Application orchestration for pin-scoped editorial workspace snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from cti_app.application.editorial_figure_catalog import (
        EditorialFigureCatalogPage,
        EditorialFigureCatalogService,
    )

from cti_app.application.editorial_legacy_read_only import (
    EditorialLegacyReadOnlyReason,
    current_editorial_read_only_reason,
)
from cti_app.application.editorial_workspace.capabilities import (
    EditorialCapabilityFacts,
    insertion_anchor_ready,
    manual_edit_anchor_ready,
    resolve_editorial_capabilities,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    WorkspaceSnapshotRecord,
    project_blocks,
    registry_hash,
    resolve_workspace_target,
)
from cti_app.application.editorial_workspace.registry import (
    resolve_occurrence as resolve_registry_occurrence,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_routing import get_adopted_production_routing_decision
from cti_app.application.subject_content import resolve_publication_kind
from cti_app.domain.editorial_proposal_impact import (
    project_editorial_impact_status,
    project_editorial_pdf_preview,
)
from cti_app.domain.editorial_workspace import (
    AdvancedBlockRegistry,
    BlockRegistryEntry,
    EditorialReadOnlyError,
    EditorialRevisionStatus,
    EditorialSourceStage,
    EditorialWorkspaceError,
    advance_block_registry,
    bootstrap_block_registry,
    reconcile_exact_block_registry,
    workspace_pin_hash,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_editorial_enrichment import (
    SourceFigureInclusionStatus,
    editorial_enrichment_from_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionTarget,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.publication_render import PublicationPreviewStatus
from cti_app.domain.typst_render import TypstRenderStatus


def _snapshot_read_only_reason(
    snapshot: Mapping[str, object],
) -> EditorialLegacyReadOnlyReason | None:
    """Read the policy value the snapshot already computed from live pinned artifacts."""
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return None
    value = capabilities.get("read_only_reason")
    if not isinstance(value, str):
        return None
    return EditorialLegacyReadOnlyReason(value)


@dataclass(frozen=True, slots=True)
class EditorialRevisionTargetResolution:
    record: WorkspaceSnapshotRecord
    occurrence: WorkspaceOccurrenceResolution
    canonical_content: Mapping[str, Any]
    target: TargetedEditorialRevisionTarget
    target_id: str
    before_hash: str


def _figure_insertion_available(
    catalog_page: EditorialFigureCatalogPage | None,
    *,
    publication_kind: PublicationKind | None,
    production_run_id: UUID,
    integrated_count: int | None,
) -> bool:
    from cti_app.application.editorial_figure_catalog import (
        EditorialFigureCatalogError,
        validate_figure_insertion_limit,
    )

    if (
        catalog_page is None
        or catalog_page.available_count < 1
        or not catalog_page.items
        or publication_kind not in {PublicationKind.ARTICLE, PublicationKind.BRIEF}
        or integrated_count is None
        or catalog_page.items[0]._run_id != production_run_id
    ):
        return False
    try:
        validate_figure_insertion_limit(
            publication_kind=publication_kind,
            integrated_count=integrated_count,
            requested_count=1,
            inventory_limit=catalog_page.items[0]._inventory_max_figures,
        )
    except EditorialFigureCatalogError:
        return False
    return True


def advance_workspace_registry(
    *,
    base_registry: tuple[BlockRegistryEntry, ...],
    artifact_id: UUID,
    canonical_sha256: str,
    source_stage: str,
    canonical_content: Mapping[str, Any],
    lineage: tuple[Mapping[str, object], ...],
    retired_block_ids: tuple[UUID, ...] = (),
) -> AdvancedBlockRegistry:
    """Prepare an append-only registry advance for a future adoption UOW."""
    return advance_block_registry(
        base_registry=base_registry,
        artifact_id=artifact_id,
        canonical_sha256=canonical_sha256,
        source_stage=EditorialSourceStage(source_stage),
        canonical_content=canonical_content,
        lineage=lineage,
        retired_block_ids=retired_block_ids,
    )


class EditorialWorkspaceService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        figure_catalog_service: EditorialFigureCatalogService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._figure_catalog_service = figure_catalog_service

    async def snapshot(self, subject_id: UUID) -> dict[str, object]:
        result, _record, _synthesis = await self._load_current(subject_id)
        return result

    async def snapshot_record_in_uow(
        self, uow: UnitOfWork, subject_id: UUID
    ) -> WorkspaceSnapshotRecord:
        """Resolve the current pins without committing outside the caller's transaction."""
        _snapshot, record, _synthesis = await self._load_current(
            subject_id,
            uow=uow,
            lock_creation=False,
            commit_snapshot=False,
        )
        return record

    async def resolve_occurrence(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        """Resolve one occurrence against the subject's current registry snapshot."""
        snapshot, record, _synthesis = await self._load_current(subject_id)
        return self._occurrence_with_policy(snapshot, record, occurrence_id)

    async def resolve_occurrence_in_uow(
        self, uow: UnitOfWork, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        """Resolve pins within a caller's transaction while it holds the run lock."""
        snapshot, record, _synthesis = await self._load_current(
            subject_id,
            uow=uow,
            lock_creation=False,
            commit_snapshot=False,
        )
        return self._occurrence_with_policy(snapshot, record, occurrence_id)

    @staticmethod
    def _occurrence_with_policy(
        snapshot: Mapping[str, object],
        record: WorkspaceSnapshotRecord,
        occurrence_id: UUID,
    ) -> WorkspaceOccurrenceResolution:
        resolution = resolve_registry_occurrence(record, occurrence_id)
        reason = _snapshot_read_only_reason(snapshot)
        if reason is None:
            return resolution
        return replace(resolution, read_only_reason=reason)

    async def resolve(
        self,
        *,
        subject_id: UUID,
        snapshot_id: UUID,
        block_id: UUID,
        operation: str,
    ) -> dict[str, object]:
        _snapshot, record, synthesis = await self._load_current(subject_id)
        if record.snapshot_id != snapshot_id:
            raise EditorialWorkspaceError("editorial_snapshot_stale")
        if record.publication_kind is None:
            raise EditorialWorkspaceError("publication_kind_unresolved")
        if operation in {
            TargetedEditorialRevisionOperation.CREATE_TABLE.value,
            TargetedEditorialRevisionOperation.CREATE_BAR_CHART.value,
            TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE.value,
        } and (
            record.enrichment_artifact_id is None
            or record.enrichment_version is None
            or record.enrichment_input_hash is None
            or record.enrichment_canonical_sha256 is None
        ):
            raise EditorialWorkspaceError("editorial_block_missing")
        occurrence = resolve_registry_occurrence(record, block_id)
        entry = occurrence.entry
        if entry is None:
            raise EditorialWorkspaceError("editorial_block_missing")
        resolved = resolve_workspace_target(
            entry=entry,
            canonical_content=synthesis,
            operation=operation,
        )
        return {
            "snapshot_id": str(record.snapshot_id),
            "block_id": str(resolved.block_id),
            "path": resolved.path,
            "before_hash": resolved.before_hash,
            "operation": resolved.operation,
        }

    async def resolve_revision_target(
        self,
        *,
        subject_id: UUID,
        snapshot_id: UUID,
        block_id: UUID,
        operation: str,
    ) -> EditorialRevisionTargetResolution:
        """Resolve the exact current occurrence and its canonical revision target."""
        snapshot, record, synthesis = await self._load_current(subject_id)
        if record.snapshot_id != snapshot_id:
            raise EditorialWorkspaceError("editorial_snapshot_stale")
        if record.publication_kind is None:
            raise EditorialWorkspaceError("publication_kind_unresolved")
        read_only_reason = _snapshot_read_only_reason(snapshot)
        if read_only_reason is not None:
            raise EditorialReadOnlyError(read_only_reason.value)
        if operation in {
            TargetedEditorialRevisionOperation.CREATE_TABLE.value,
            TargetedEditorialRevisionOperation.CREATE_BAR_CHART.value,
            TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE.value,
        } and (
            record.enrichment_artifact_id is None
            or record.enrichment_version is None
            or record.enrichment_input_hash is None
            or record.enrichment_canonical_sha256 is None
        ):
            raise EditorialWorkspaceError("editorial_block_missing")
        occurrence = resolve_registry_occurrence(record, block_id)
        if occurrence.entry is None:
            raise EditorialWorkspaceError("editorial_block_missing")
        if occurrence.binding_state == "stale":
            raise EditorialWorkspaceError("editorial_snapshot_stale")
        if occurrence.binding_state != "valid":
            raise EditorialWorkspaceError("editorial_identity_ambiguous")
        resolved = resolve_workspace_target(
            entry=occurrence.entry,
            canonical_content=synthesis,
            operation=operation,
        )
        if resolved.legacy_target_id is None:
            raise EditorialWorkspaceError("editorial_identity_ambiguous")
        return EditorialRevisionTargetResolution(
            record=record,
            occurrence=occurrence,
            canonical_content=synthesis,
            target=resolved.target,
            target_id=resolved.legacy_target_id,
            before_hash=resolved.before_hash,
        )

    def advance(
        self,
        *,
        base_registry: tuple[BlockRegistryEntry, ...],
        artifact_id: UUID,
        canonical_sha256: str,
        source_stage: str,
        canonical_content: Mapping[str, Any],
        lineage: tuple[Mapping[str, object], ...],
        retired_block_ids: tuple[UUID, ...] = (),
    ) -> AdvancedBlockRegistry:
        """Advance explicit identity lineage; adoption does not call it yet."""
        return advance_workspace_registry(
            base_registry=base_registry,
            artifact_id=artifact_id,
            canonical_sha256=canonical_sha256,
            source_stage=EditorialSourceStage(source_stage),
            canonical_content=canonical_content,
            lineage=lineage,
            retired_block_ids=retired_block_ids,
        )

    async def _load_current(
        self,
        subject_id: UUID,
        *,
        uow: UnitOfWork | None = None,
        lock_creation: bool = True,
        commit_snapshot: bool = True,
    ) -> tuple[dict[str, object], WorkspaceSnapshotRecord, Mapping[str, Any]]:
        figure_catalog_page: EditorialFigureCatalogPage | None = None
        figure_catalog_run_id: UUID | None = None
        catalog_figure_ids: dict[UUID, str] = {}
        catalog_figure_captions: dict[UUID, str | None] = {}
        if uow is None and self._figure_catalog_service is not None:
            from cti_app.application.editorial_figure_catalog import EditorialFigureCatalogError

            try:
                # Figure insertion is a manual operation and is advertised only
                # when the current archived inventory has a selectable item.
                figure_catalog_page = await self._figure_catalog_service.catalog(
                    subject_id=subject_id,
                    limit=1,
                )
                (
                    figure_catalog_run_id,
                    catalog_figure_items,
                ) = await self._figure_catalog_service.source_figure_catalog_figures(subject_id)
                catalog_figure_ids = {
                    source_id: item.figure_id for source_id, item in catalog_figure_items.items()
                }
                catalog_figure_captions = {
                    source_id: item.original_caption
                    for source_id, item in catalog_figure_items.items()
                }
            except EditorialFigureCatalogError:
                figure_catalog_page = None
                figure_catalog_run_id = None
                catalog_figure_ids = {}
                catalog_figure_captions = {}

        async with self._uow_scope(uow) as uow:
            subject = await uow.subjects.get(subject_id)
            if subject is None:
                raise EditorialWorkspaceError("subject_not_found")
            if lock_creation:
                await uow.production_runs.lock_creation_for_subject(subject_id)
            run = await uow.production_runs.get_current_for_subject(subject_id)
            if run is None:
                raise EditorialWorkspaceError("production_run_missing")
            if figure_catalog_run_id != run.id:
                catalog_figure_ids = {}
                catalog_figure_captions = {}
            synthesis_artifact = await uow.production_artifacts.get_current_for_revision(
                run.id, ProductionArtifactStage.SYNTHESIS.value
            )
            if synthesis_artifact is None:
                raise EditorialWorkspaceError("editorial_snapshot_stale")
            enrichment_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
            )
            publication_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.PUBLICATION.value
            )
            synthesis, synthesis_sha = await self._read_artifact(synthesis_artifact)
            enrichment_content: Mapping[str, Any] | None = None
            enrichment_sha: str | None = None
            if enrichment_artifact is not None:
                enrichment_content, enrichment_sha = await self._read_artifact(enrichment_artifact)
            publication_content: Mapping[str, Any] | None = None
            publication_sha: str | None = None
            if publication_artifact is not None:
                publication_content, publication_sha = await self._read_artifact(
                    publication_artifact
                )

            routing_decision = await get_adopted_production_routing_decision(uow=uow, run=run)
            kind, routing_hash = resolve_publication_kind(routing_decision, publication_content)
            read_only_reason = current_editorial_read_only_reason(
                routing_decision=routing_decision,
                publication_content=publication_content,
                publication_kind=kind,
                synthesis_content=synthesis,
                enrichment_content=enrichment_content,
            )
            pins: dict[str, object] = {
                "subject_id": subject.id,
                "production_run_id": run.id,
                "synthesis_artifact_id": synthesis_artifact.id,
                "synthesis_version": synthesis_artifact.version,
                "synthesis_input_hash": synthesis_artifact.input_hash,
                "synthesis_canonical_sha256": synthesis_sha,
                "enrichment_artifact_id": enrichment_artifact.id if enrichment_artifact else None,
                "enrichment_version": enrichment_artifact.version if enrichment_artifact else None,
                "enrichment_input_hash": enrichment_artifact.input_hash
                if enrichment_artifact
                else None,
                "enrichment_canonical_sha256": enrichment_sha,
                "publication_artifact_id": publication_artifact.id
                if publication_artifact
                else None,
                "publication_version": publication_artifact.version
                if publication_artifact
                else None,
                "publication_input_hash": publication_artifact.input_hash
                if publication_artifact
                else None,
                "publication_canonical_sha256": publication_sha,
                "pipeline_generation": run.pipeline_generation,
                "routing_decision_id": routing_decision.decision_id if routing_decision else None,
                "routing_decision_hash": routing_hash,
                "publication_kind": kind,
            }
            pin_hash = workspace_pin_hash(pins)
            repository = uow.editorial_workspace_snapshots
            stored_record = await repository.get_by_pin(run.id, pin_hash)
            should_commit = stored_record is None
            if stored_record is None:
                entries = list(
                    bootstrap_block_registry(
                        artifact_id=synthesis_artifact.id,
                        canonical_sha256=synthesis_sha,
                        source_stage=ProductionArtifactStage.SYNTHESIS.value,
                        canonical_content=synthesis,
                    )
                )
                if enrichment_artifact is not None and enrichment_content is not None:
                    entries.extend(
                        bootstrap_block_registry(
                            artifact_id=enrichment_artifact.id,
                            canonical_sha256=enrichment_sha or "0" * 64,
                            source_stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT.value,
                            canonical_content=enrichment_content,
                        )
                    )
                previous_record = await repository.latest_for_run(run.id)
                if previous_record is not None:
                    synthesis_unchanged = (
                        previous_record.synthesis_artifact_id == synthesis_artifact.id
                        and previous_record.synthesis_version == synthesis_artifact.version
                        and previous_record.synthesis_canonical_sha256 == synthesis_sha
                    )
                    enrichment_unchanged = (
                        previous_record.enrichment_artifact_id
                        == (enrichment_artifact.id if enrichment_artifact else None)
                        and previous_record.enrichment_version
                        == (enrichment_artifact.version if enrichment_artifact else None)
                        and previous_record.enrichment_canonical_sha256 == enrichment_sha
                    )
                    entries = self._reconcile_changed_stage(
                        entries=entries,
                        previous_record=previous_record,
                        stage=EditorialSourceStage.SYNTHESIS,
                        unchanged=synthesis_unchanged,
                        current_version=synthesis_artifact.version,
                        previous_version=previous_record.synthesis_version,
                    )
                    entries = self._reconcile_changed_stage(
                        entries=entries,
                        previous_record=previous_record,
                        stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
                        unchanged=enrichment_unchanged,
                        current_version=(
                            enrichment_artifact.version if enrichment_artifact else None
                        ),
                        previous_version=previous_record.enrichment_version,
                    )
                record = WorkspaceSnapshotRecord(
                    snapshot_id=uuid4(),
                    subject_id=subject.id,
                    production_run_id=run.id,
                    synthesis_artifact_id=synthesis_artifact.id,
                    synthesis_version=synthesis_artifact.version,
                    synthesis_input_hash=synthesis_artifact.input_hash,
                    synthesis_canonical_sha256=synthesis_sha,
                    enrichment_artifact_id=enrichment_artifact.id if enrichment_artifact else None,
                    enrichment_version=enrichment_artifact.version if enrichment_artifact else None,
                    enrichment_input_hash=enrichment_artifact.input_hash
                    if enrichment_artifact
                    else None,
                    enrichment_canonical_sha256=enrichment_sha,
                    publication_artifact_id=publication_artifact.id
                    if publication_artifact
                    else None,
                    publication_version=publication_artifact.version
                    if publication_artifact
                    else None,
                    publication_input_hash=publication_artifact.input_hash
                    if publication_artifact
                    else None,
                    publication_canonical_sha256=publication_sha,
                    pipeline_generation=run.pipeline_generation,
                    routing_decision_id=routing_decision.decision_id if routing_decision else None,
                    routing_decision_hash=routing_hash,
                    publication_kind=kind,
                    workspace_pin_hash=pin_hash,
                    registry_hash=registry_hash(entries),
                    block_registry=tuple(entries),
                    parent_snapshot_id=(
                        previous_record.snapshot_id if previous_record is not None else None
                    ),
                )
                await self._assert_artifact_pointers_current(
                    uow=uow,
                    subject_id=subject_id,
                    production_run_id=run.id,
                    pipeline_generation=run.pipeline_generation,
                    synthesis_artifact=synthesis_artifact,
                    enrichment_artifact=enrichment_artifact,
                    publication_artifact=publication_artifact,
                )
                stored_record = await repository.append_if_absent(record)
                if (
                    stored_record.registry_hash != record.registry_hash
                    or stored_record.block_registry != record.block_registry
                ):
                    raise EditorialWorkspaceError("editorial_registry_conflict")
                record = stored_record
            else:
                record = stored_record

            entries = list(record.block_registry)
            self._assert_record_matches_current(
                record,
                pins=pins,
                workspace_pin_hash_value=pin_hash,
            )
            integrated_figure_count: int | None = None
            if enrichment_content is not None:
                try:
                    parsed_enrichment = editorial_enrichment_from_json(enrichment_content)
                except (TypeError, ValueError):
                    pass
                else:
                    integrated_figure_count = sum(
                        figure.inclusion_status is SourceFigureInclusionStatus.INCLUDED
                        for figure in parsed_enrichment.source_figures
                    )
            figure_inventory_available = _figure_insertion_available(
                figure_catalog_page,
                publication_kind=kind,
                production_run_id=run.id,
                integrated_count=integrated_figure_count,
            )
            blocks, enrichments, outline = project_blocks(
                entries=entries,
                synthesis=synthesis,
                enrichment=enrichment_content,
                record=record,
                synthesis_artifact=synthesis_artifact,
                synthesis_read_sha256=synthesis_sha,
                enrichment_artifact=enrichment_artifact,
                enrichment_read_sha256=enrichment_sha,
                allow_operations=kind is not None,
                allow_figure_insertion=kind is not None and figure_inventory_available,
                catalog_figure_ids=catalog_figure_ids,
                catalog_figure_captions=catalog_figure_captions,
            )
            if read_only_reason is not None:
                reason = read_only_reason.value
                for block in (*blocks, *enrichments):
                    block["allowed_operations"] = []
                    block["read_only_reason"] = reason
            impact = await uow.editorial_proposal_impacts.latest_for_run(run.id)
            publication_render = (
                await uow.publication_renders.get_latest_for_artifact(publication_artifact.id)
                if publication_artifact is not None
                else None
            )
            publication_stale = False
            if publication_artifact is None:
                publication_history = [
                    item
                    for item in await uow.production_artifacts.list_for_run(run.id)
                    if item.stage == ProductionArtifactStage.PUBLICATION
                ]
                latest_publication = max(
                    publication_history,
                    key=lambda item: item.version,
                    default=None,
                )
                publication_stale = (
                    latest_publication is not None
                    and latest_publication.status is ProductionArtifactStatus.STALE
                )
            else:
                publication_stale = publication_artifact.status is ProductionArtifactStatus.STALE
                input_artifacts = publication_artifact.metadata.get("input_artifacts")
                if isinstance(input_artifacts, Mapping):
                    current_enrichment_id = (
                        str(enrichment_artifact.id) if enrichment_artifact is not None else None
                    )
                    publication_stale = (
                        publication_stale
                        or (
                            "synthesis_artifact_id" in input_artifacts
                            and input_artifacts.get("synthesis_artifact_id")
                            != str(synthesis_artifact.id)
                        )
                        or (
                            input_artifacts.get("editorial_enrichment_artifact_id")
                            != current_enrichment_id
                        )
                    )
            publication_stale = publication_stale or any(
                enrichment.get("revision_status") == "stale" for enrichment in enrichments
            )
            render_status = (
                {
                    TypstRenderStatus.RUNNING: PublicationPreviewStatus.IN_PROGRESS,
                    TypstRenderStatus.SUCCEEDED: PublicationPreviewStatus.READY,
                    TypstRenderStatus.FAILED: PublicationPreviewStatus.FAILED,
                }[publication_render.status]
                if publication_render is not None
                else None
            )
            impact_status, preview_status, pdf_status = project_editorial_impact_status(
                impact,
                render_status=render_status,
                publication_stale=publication_stale,
            )
            pdf_preview = project_editorial_pdf_preview(
                subject_id=subject.id,
                current_artifact=publication_artifact,
                current_canonical_sha256=publication_sha,
                render=publication_render,
                publication_stale=publication_stale,
            )
            # The service only knows document facts. Request wiring (Bridge worker,
            # accept writer, thread routes) is overlaid by the HTTP route with the
            # same resolver; without it every mutating capability is fail-closed.
            capabilities = resolve_editorial_capabilities(
                EditorialCapabilityFacts(
                    publication_kind_known=kind is not None,
                    production_run_known=True,
                    read_only=read_only_reason is not None,
                    manual_edit_anchor_ready=manual_edit_anchor_ready(blocks),
                    insertion_anchor_ready=insertion_anchor_ready(blocks),
                )
            )
            result: dict[str, object] = {
                "subject_id": str(subject.id),
                "production_run_id": str(run.id),
                "publication_kind": kind.value if kind else None,
                "synthesis_artifact_id": str(synthesis_artifact.id),
                "synthesis_version": synthesis_artifact.version,
                "synthesis_canonical_sha256": synthesis_sha,
                "enrichment_artifact_id": (
                    str(enrichment_artifact.id) if enrichment_artifact is not None else None
                ),
                "enrichment_version": (
                    enrichment_artifact.version if enrichment_artifact is not None else None
                ),
                "publication_artifact_id": (
                    str(publication_artifact.id) if publication_artifact is not None else None
                ),
                "publication_version": (
                    publication_artifact.version if publication_artifact is not None else None
                ),
                "pipeline_generation": run.pipeline_generation,
                "blocks": blocks,
                "enrichments": enrichments,
                "document_outline": outline,
                "capabilities": {
                    "can_discuss": capabilities.can_discuss,
                    "can_preview_revision": capabilities.can_preview_revision,
                    "manual_edit": capabilities.manual_edit,
                    "can_add_enrichment": capabilities.can_add_enrichment,
                    "can_preview_pdf": (
                        read_only_reason is None and pdf_preview.status.value == "READY"
                    ),
                    "can_regenerate_pdf": kind is not None and read_only_reason is None,
                    "read_only_reason": (
                        read_only_reason.value if read_only_reason is not None else None
                    ),
                },
                "preview_status": preview_status.value,
                "impact_status": impact_status,
                "pdf_status": pdf_status,
                "pdf_artifact_id": (
                    str(pdf_preview.artifact_id) if pdf_preview.artifact_id is not None else None
                ),
                "pdf_artifact_version": pdf_preview.artifact_version,
                "pdf_artifact_input_hash": pdf_preview.artifact_input_hash,
                "pdf_artifact_canonical_sha256": pdf_preview.artifact_canonical_sha256,
                "pdf_render_id": (
                    str(pdf_preview.render_id) if pdf_preview.render_id is not None else None
                ),
                "pdf_render_identity": pdf_preview.render_identity,
                "pdf_error_code": pdf_preview.error_code,
                "pdf_error_message": pdf_preview.error_message,
                "pdf_url": pdf_preview.pdf_url,
                "snapshot_id": str(record.snapshot_id),
            }
            await self._assert_artifact_pointers_current(
                uow=uow,
                subject_id=subject_id,
                production_run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                synthesis_artifact=synthesis_artifact,
                enrichment_artifact=enrichment_artifact,
                publication_artifact=publication_artifact,
            )
            if should_commit and commit_snapshot:
                await uow.commit()
            return result, record, synthesis

    @asynccontextmanager
    async def _uow_scope(self, existing_uow: UnitOfWork | None) -> AsyncIterator[UnitOfWork]:
        if existing_uow is None:
            async with self._uow_factory() as uow:
                yield uow
        else:
            yield existing_uow

    @staticmethod
    def _reconcile_changed_stage(
        *,
        entries: list[BlockRegistryEntry],
        previous_record: WorkspaceSnapshotRecord,
        stage: EditorialSourceStage,
        unchanged: bool,
        current_version: int | None,
        previous_version: int | None,
    ) -> list[BlockRegistryEntry]:
        current_stage = [entry for entry in entries if entry.source_stage is stage]
        if unchanged:
            previous_stage = [
                entry for entry in previous_record.block_registry if entry.source_stage is stage
            ]
            previous_by_pointer = {entry.structural_pointer: entry for entry in previous_stage}
            current_by_pointer = {entry.structural_pointer: entry for entry in current_stage}
            if previous_by_pointer.keys() != current_by_pointer.keys():
                return entries

            previous_pointer_by_id = {
                entry.block_id: entry.structural_pointer for entry in previous_stage
            }
            current_pointer_by_id = {
                entry.block_id: entry.structural_pointer for entry in current_stage
            }
            for pointer, current in current_by_pointer.items():
                previous = previous_by_pointer[pointer]
                previous_parent_pointer = (
                    previous_pointer_by_id.get(previous.parent_block_id)
                    if previous.parent_block_id is not None
                    else None
                )
                current_parent_pointer = (
                    current_pointer_by_id.get(current.parent_block_id)
                    if current.parent_block_id is not None
                    else None
                )
                if (
                    previous.kind is not current.kind
                    or previous.position != current.position
                    or previous.content_sha256 != current.content_sha256
                    or previous_parent_pointer != current_parent_pointer
                ):
                    return entries

            replacements = {
                current.block_id: previous_by_pointer[pointer]
                for pointer, current in current_by_pointer.items()
            }
            return [replacements.get(entry.block_id, entry) for entry in entries]
        previous_stage = [
            entry for entry in previous_record.block_registry if entry.source_stage is stage
        ]
        if (
            previous_stage
            and current_stage
            and current_version is not None
            and previous_version is not None
            and current_version > previous_version
        ):
            reconciled_stage = reconcile_exact_block_registry(previous_stage, current_stage)
        else:
            reconciled_stage = tuple(
                replace(entry, revision_status=EditorialRevisionStatus.NEEDS_REVIEW)
                for entry in current_stage
            )
        replacements = {
            current.block_id: reconciled
            for current, reconciled in zip(current_stage, reconciled_stage, strict=True)
        }
        return [replacements.get(entry.block_id, entry) for entry in entries]

    @staticmethod
    async def _assert_artifact_pointers_current(
        *,
        uow: UnitOfWork,
        subject_id: UUID,
        production_run_id: UUID,
        pipeline_generation: int,
        synthesis_artifact: ProductionArtifact,
        enrichment_artifact: ProductionArtifact | None,
        publication_artifact: ProductionArtifact | None,
    ) -> None:
        current_run = await uow.production_runs.get_current_for_subject(subject_id)
        if (
            current_run is None
            or current_run.id != production_run_id
            or current_run.pipeline_generation != pipeline_generation
        ):
            raise EditorialWorkspaceError("editorial_snapshot_stale")
        current_synthesis = await uow.production_artifacts.get_current_for_revision(
            production_run_id, ProductionArtifactStage.SYNTHESIS.value
        )
        current_enrichment = await uow.production_artifacts.get_current(
            production_run_id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
        current_publication = await uow.production_artifacts.get_current(
            production_run_id, ProductionArtifactStage.PUBLICATION.value
        )
        expected = (synthesis_artifact, enrichment_artifact, publication_artifact)
        actual = (current_synthesis, current_enrichment, current_publication)
        if any(
            (expected_item is None) != (actual_item is None)
            or (
                expected_item is not None
                and actual_item is not None
                and (expected_item.id, expected_item.version)
                != (actual_item.id, actual_item.version)
            )
            for expected_item, actual_item in zip(expected, actual, strict=True)
        ):
            raise EditorialWorkspaceError("editorial_snapshot_stale")
        # T-M3-15's writer cutover closes the unlocked legacy artifact path.

    async def _read_artifact(self, artifact: ProductionArtifact) -> tuple[dict[str, Any], str]:
        if artifact.canonical_blob_id is None:
            raise EditorialWorkspaceError("editorial_snapshot_stale")
        content = await self._artifact_store.read_bytes(artifact.canonical_blob_id)
        canonical_sha = hashlib.sha256(content).hexdigest()
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EditorialWorkspaceError("editorial_snapshot_stale") from exc
        if not isinstance(payload, dict):
            raise EditorialWorkspaceError("editorial_snapshot_stale")
        return payload, canonical_sha

    @staticmethod
    def _assert_record_matches_current(
        record: WorkspaceSnapshotRecord,
        *,
        pins: Mapping[str, object],
        workspace_pin_hash_value: str,
    ) -> None:
        stored_values: dict[str, object] = {
            "production_run_id": record.production_run_id,
            "synthesis_artifact_id": record.synthesis_artifact_id,
            "synthesis_version": record.synthesis_version,
            "synthesis_input_hash": record.synthesis_input_hash,
            "synthesis_canonical_sha256": record.synthesis_canonical_sha256,
            "enrichment_artifact_id": record.enrichment_artifact_id,
            "enrichment_version": record.enrichment_version,
            "enrichment_input_hash": record.enrichment_input_hash,
            "enrichment_canonical_sha256": record.enrichment_canonical_sha256,
            "publication_artifact_id": record.publication_artifact_id,
            "publication_version": record.publication_version,
            "publication_input_hash": record.publication_input_hash,
            "publication_canonical_sha256": record.publication_canonical_sha256,
            "pipeline_generation": record.pipeline_generation,
            "routing_decision_id": record.routing_decision_id,
            "routing_decision_hash": record.routing_decision_hash,
            "publication_kind": record.publication_kind,
        }
        if (
            record.workspace_pin_hash != workspace_pin_hash_value
            or record.subject_id != pins.get("subject_id")
            or any(pins.get(name) != value for name, value in stored_values.items())
        ):
            raise EditorialWorkspaceError("editorial_registry_conflict")

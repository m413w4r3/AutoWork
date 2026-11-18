"""Application orchestration for pin-scoped editorial workspace snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    project_blocks,
    registry_hash,
    resolve_workspace_target,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_routing import get_adopted_production_routing_decision
from cti_app.domain.editorial_workspace import (
    AdvancedBlockRegistry,
    BlockRegistryEntry,
    EditorialRevisionStatus,
    EditorialSourceStage,
    EditorialWorkspaceError,
    advance_block_registry,
    bootstrap_block_registry,
    reconcile_exact_block_registry,
    workspace_pin_hash,
)
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from cti_app.domain.production_routing import ProductionRoutingDecisionV1
from cti_app.domain.publication_document import parse_publication_document
from cti_app.domain.publication_kind import PublicationKind


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
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def snapshot(self, subject_id: UUID) -> dict[str, object]:
        result, _record, _synthesis = await self._load_current(subject_id)
        return result

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
        entries = record.block_registry
        entry = next((item for item in entries if item.block_id == block_id), None)
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
        self, subject_id: UUID
    ) -> tuple[dict[str, object], WorkspaceSnapshotRecord, Mapping[str, Any]]:
        async with self._uow_factory() as uow:
            subject = await uow.subjects.get(subject_id)
            if subject is None:
                raise EditorialWorkspaceError("subject_not_found")
            await uow.production_runs.lock_creation_for_subject(subject_id)
            run = await uow.production_runs.get_current_for_subject(subject_id)
            if run is None:
                raise EditorialWorkspaceError("production_run_missing")
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
            kind, routing_hash = self._publication_kind(routing_decision, publication_content)
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
            blocks, enrichments, outline = project_blocks(
                entries=entries,
                synthesis=synthesis,
                enrichment=enrichment_content,
                allow_operations=kind is not None,
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
                    "can_discuss": False,
                    "can_preview_revision": False,
                    "manual_edit": False,
                    "can_add_enrichment": False,
                    "can_preview_pdf": False,
                },
                "preview_status": "READY",
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
            if should_commit:
                await uow.commit()
            return result, record, synthesis

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
            return entries
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
    def _publication_kind(
        routing_decision: ProductionRoutingDecisionV1 | None,
        publication_content: Mapping[str, Any] | None,
    ) -> tuple[PublicationKind | None, str | None]:
        routing_kind = routing_decision.publication_kind if routing_decision else None
        routing_hash = routing_decision.decision_hash if routing_decision else None
        envelope_kind: PublicationKind | None = None
        envelope_hash: str | None = None
        if publication_content is not None:
            try:
                publication = parse_publication_document(publication_content)
            except ValueError:
                publication = None
            raw_kind = getattr(publication, "publication_kind", None)
            if isinstance(raw_kind, PublicationKind):
                envelope_kind = raw_kind
                raw_hash = getattr(publication, "routing_decision_hash", None)
                if isinstance(raw_hash, str):
                    envelope_hash = raw_hash
        if (
            routing_kind is not None
            and envelope_kind is not None
            and routing_kind is not envelope_kind
        ):
            return None, routing_hash
        if routing_hash is not None and envelope_hash is not None and routing_hash != envelope_hash:
            return None, routing_hash
        if routing_kind is not None:
            return routing_kind, routing_hash
        return envelope_kind, envelope_hash

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

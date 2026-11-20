from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    registry_from_json,
    registry_hash,
)
from cti_app.domain.editorial_workspace import (
    EditorialRegistryConflictError,
    validate_block_registry,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.database.models.editorial_workspace import (
    EditorialWorkspaceSnapshotRow,
)


class SqlAlchemyEditorialWorkspaceSnapshotRepository:
    """PostgreSQL adapter for immutable workspace registry snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_pin(
        self, production_run_id: UUID, workspace_pin_hash: str
    ) -> WorkspaceSnapshotRecord | None:
        result = await self._session.execute(
            select(EditorialWorkspaceSnapshotRow).where(
                EditorialWorkspaceSnapshotRow.production_run_id == production_run_id,
                EditorialWorkspaceSnapshotRow.workspace_pin_hash == workspace_pin_hash,
            )
        )
        row = result.scalar_one_or_none()
        return _record_from_row(row) if row is not None else None

    async def get_by_id(self, snapshot_id: UUID) -> WorkspaceSnapshotRecord | None:
        row = await self._session.get(EditorialWorkspaceSnapshotRow, snapshot_id)
        return _record_from_row(row) if row is not None else None

    async def latest_for_run(self, production_run_id: UUID) -> WorkspaceSnapshotRecord | None:
        result = await self._session.execute(
            select(EditorialWorkspaceSnapshotRow)
            .where(EditorialWorkspaceSnapshotRow.production_run_id == production_run_id)
            .order_by(
                EditorialWorkspaceSnapshotRow.created_at.desc(),
                EditorialWorkspaceSnapshotRow.snapshot_id.desc(),
            )
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _record_from_row(row) if row is not None else None

    async def list_for_run(self, production_run_id: UUID) -> tuple[WorkspaceSnapshotRecord, ...]:
        result = await self._session.execute(
            select(EditorialWorkspaceSnapshotRow)
            .where(EditorialWorkspaceSnapshotRow.production_run_id == production_run_id)
            .order_by(
                EditorialWorkspaceSnapshotRow.created_at,
                EditorialWorkspaceSnapshotRow.snapshot_id,
            )
        )
        return tuple(_record_from_row(row) for row in result.scalars().all())

    async def append_if_absent(self, record: WorkspaceSnapshotRecord) -> WorkspaceSnapshotRecord:
        values = _row_values(record)
        statement = insert(EditorialWorkspaceSnapshotRow).values(**values)
        statement = statement.on_conflict_do_nothing(
            constraint="uq_editorial_workspace_snapshots_run_pin"
        )
        await self._session.execute(statement)
        stored = await self.get_by_pin(record.production_run_id, record.workspace_pin_hash)
        if stored is None:
            raise RuntimeError("editorial_registry_insert_missing")
        return stored


def _row_values(record: WorkspaceSnapshotRecord) -> dict[str, object]:
    return {
        "snapshot_id": record.snapshot_id,
        "subject_id": record.subject_id,
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
        "publication_kind": (
            record.publication_kind.value if record.publication_kind is not None else None
        ),
        "workspace_pin_hash": record.workspace_pin_hash,
        "registry_hash": record.registry_hash,
        "block_registry": [entry.to_json() for entry in record.block_registry],
        "retired_block_ids": [str(item) for item in record.retired_block_ids],
        "parent_snapshot_id": record.parent_snapshot_id,
    }


def _record_from_row(row: EditorialWorkspaceSnapshotRow) -> WorkspaceSnapshotRecord:
    try:
        entries = registry_from_json(row.block_registry)
        retired = tuple(UUID(value) for value in row.retired_block_ids)
        validate_block_registry(entries, retired)
        record = WorkspaceSnapshotRecord(
            snapshot_id=row.snapshot_id,
            subject_id=row.subject_id,
            production_run_id=row.production_run_id,
            synthesis_artifact_id=row.synthesis_artifact_id,
            synthesis_version=row.synthesis_version,
            synthesis_input_hash=row.synthesis_input_hash,
            synthesis_canonical_sha256=row.synthesis_canonical_sha256,
            enrichment_artifact_id=row.enrichment_artifact_id,
            enrichment_version=row.enrichment_version,
            enrichment_input_hash=row.enrichment_input_hash,
            enrichment_canonical_sha256=row.enrichment_canonical_sha256,
            publication_artifact_id=row.publication_artifact_id,
            publication_version=row.publication_version,
            publication_input_hash=row.publication_input_hash,
            publication_canonical_sha256=row.publication_canonical_sha256,
            pipeline_generation=row.pipeline_generation,
            routing_decision_id=row.routing_decision_id,
            routing_decision_hash=row.routing_decision_hash,
            publication_kind=(
                PublicationKind(row.publication_kind) if row.publication_kind is not None else None
            ),
            workspace_pin_hash=row.workspace_pin_hash,
            registry_hash=row.registry_hash,
            block_registry=entries,
            retired_block_ids=retired,
            parent_snapshot_id=row.parent_snapshot_id,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EditorialRegistryConflictError("editorial_registry_conflict") from exc
    if registry_hash(record.block_registry) != record.registry_hash:
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    return record

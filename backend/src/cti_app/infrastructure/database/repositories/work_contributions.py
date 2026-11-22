"""SQLAlchemy repository for the append-only work contribution rows."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.investigation_persistence import (
    VersionConflictError,
    same_work_contribution_request,
)
from cti_app.domain.work_contribution import (
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionOrigin,
    WorkContributionV1,
    WorkContributionValidationCode,
)
from cti_app.infrastructure.database.models.work_contributions import (
    WorkContributionRow,
)


def _from_row(row: WorkContributionRow) -> WorkContributionV1:
    return WorkContributionV1(
        id=row.id,
        investigation_id=row.investigation_id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        category=WorkContributionCategory(row.category),
        schema_version=row.schema_version,
        workspace_revision_id=row.workspace_revision_id,
        file_relpath=row.file_relpath,
        file_sha256=row.file_sha256,
        observed_at=row.observed_at,
        origin=WorkContributionOrigin(row.origin),
        origin_actor_id=row.origin_actor_id,
        lease_fence=row.lease_fence,
        content_blob_id=row.content_blob_id,
        content_sha256=row.content_sha256,
        import_status=WorkContributionImportStatus(row.import_status),
        observation_id=row.observation_id,
        finding_id=row.finding_id,
        sample_association_id=row.sample_association_id,
        validation_error_code=(
            WorkContributionValidationCode(row.validation_error_code)
            if row.validation_error_code is not None
            else None
        ),
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
    )


def _values(contribution: WorkContributionV1) -> dict[str, object]:
    return {
        "id": contribution.id,
        "investigation_id": contribution.investigation_id,
        "workspace_id": contribution.workspace_id,
        "task_id": contribution.task_id,
        "category": contribution.category.value,
        "schema_version": contribution.schema_version,
        "workspace_revision_id": contribution.workspace_revision_id,
        "file_relpath": contribution.file_relpath,
        "file_sha256": contribution.file_sha256,
        "observed_at": contribution.observed_at,
        "origin": contribution.origin.value,
        "origin_actor_id": contribution.origin_actor_id,
        "lease_fence": contribution.lease_fence,
        "content_blob_id": contribution.content_blob_id,
        "content_sha256": contribution.content_sha256,
        "import_status": contribution.import_status.value,
        "observation_id": contribution.observation_id,
        "finding_id": contribution.finding_id,
        "sample_association_id": contribution.sample_association_id,
        "validation_error_code": (
            contribution.validation_error_code.value
            if contribution.validation_error_code is not None
            else None
        ),
        "idempotency_key": contribution.idempotency_key,
        "created_at": contribution.created_at,
    }


class SqlAlchemyWorkContributionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, contribution_id: UUID) -> WorkContributionV1 | None:
        row = await self._session.get(WorkContributionRow, contribution_id)
        return _from_row(row) if row is not None else None

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> WorkContributionV1 | None:
        row = await self._session.scalar(
            select(WorkContributionRow).where(
                WorkContributionRow.investigation_id == investigation_id,
                WorkContributionRow.idempotency_key == idempotency_key,
            )
        )
        return _from_row(row) if row is not None else None

    async def get_non_rejected_for_revision(
        self, workspace_id: str, workspace_revision_id: str
    ) -> WorkContributionV1 | None:
        row = await self._session.scalar(
            select(WorkContributionRow).where(
                WorkContributionRow.workspace_id == workspace_id,
                WorkContributionRow.workspace_revision_id == workspace_revision_id,
                WorkContributionRow.import_status != WorkContributionImportStatus.REJECTED.value,
            )
        )
        return _from_row(row) if row is not None else None

    async def list_for_investigation(
        self,
        investigation_id: UUID,
        *,
        category: WorkContributionCategory | None = None,
        import_status: WorkContributionImportStatus | None = None,
        workspace_id: str | None = None,
    ) -> tuple[WorkContributionV1, ...]:
        statement = select(WorkContributionRow).where(
            WorkContributionRow.investigation_id == investigation_id
        )
        if category is not None:
            statement = statement.where(WorkContributionRow.category == category.value)
        if import_status is not None:
            statement = statement.where(WorkContributionRow.import_status == import_status.value)
        if workspace_id is not None:
            statement = statement.where(WorkContributionRow.workspace_id == workspace_id)
        statement = statement.order_by(WorkContributionRow.created_at, WorkContributionRow.id)
        rows = await self._session.scalars(statement)
        return tuple(_from_row(row) for row in rows)

    async def append_if_absent(
        self, contribution: WorkContributionV1
    ) -> tuple[WorkContributionV1, bool]:
        result = await self._session.execute(
            insert(WorkContributionRow).values(**_values(contribution)).on_conflict_do_nothing()
        )
        created = int(getattr(result, "rowcount", 0) or 0) == 1
        stored = await self.get(contribution.id)
        if created:
            if stored is None or stored != contribution:
                raise RuntimeError("work contribution insert did not produce its row")
            return stored, True
        replay = await self.get_by_idempotency(
            contribution.investigation_id, contribution.idempotency_key
        )
        if replay is not None:
            if not same_work_contribution_request(replay, contribution):
                raise VersionConflictError(
                    "Work contribution idempotency key records a different request"
                )
            return replay, False
        if contribution.import_status is not WorkContributionImportStatus.REJECTED:
            active = await self.get_non_rejected_for_revision(
                contribution.workspace_id, contribution.workspace_revision_id
            )
            if active is not None:
                return active, False
        raise VersionConflictError("work contribution conflicts with a different row")

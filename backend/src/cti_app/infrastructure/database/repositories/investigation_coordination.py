"""SQLAlchemy repository for append-only investigation plan versions (M24-04A1)."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.domain.investigation_plan import (
    InvestigationPlanHead,
    InvestigationPlanV1,
    InvestigationPlanVersion,
    PlanParseStatus,
)
from cti_app.infrastructure.database.models.investigation_coordination import (
    InvestigationPlanRow,
    InvestigationPlanVersionRow,
)


def _head_from_row(row: InvestigationPlanRow) -> InvestigationPlanHead:
    return InvestigationPlanHead(
        investigation_id=row.investigation_id,
        current_version=row.current_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _version_from_row(row: InvestigationPlanVersionRow) -> InvestigationPlanVersion:
    version = InvestigationPlanVersion(
        id=row.id,
        investigation_id=row.investigation_id,
        version=row.version,
        content=InvestigationPlanV1.from_payload(row.content),
        parse_status=PlanParseStatus(row.parse_status),
        source_invocation_id=row.source_invocation_id,
        source_observation_id=row.source_observation_id,
        supersedes_id=row.supersedes_id,
        created_at=row.created_at,
    )
    if row.content_sha256 != version.content_sha256:
        raise ValueError("Stored plan content does not match its canonical SHA-256")
    return version


def _version_row(version: InvestigationPlanVersion) -> InvestigationPlanVersionRow:
    return InvestigationPlanVersionRow(
        id=version.id,
        investigation_id=version.investigation_id,
        version=version.version,
        schema_version=version.schema_version,
        content=version.content.to_payload(),
        content_sha256=version.content_sha256,
        parse_status=version.parse_status.value,
        source_invocation_id=version.source_invocation_id,
        source_observation_id=version.source_observation_id,
        supersedes_id=version.supersedes_id,
        created_at=version.created_at,
    )


class SqlAlchemyInvestigationPlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_head(self, investigation_id: UUID) -> InvestigationPlanHead | None:
        row = await self._session.get(InvestigationPlanRow, investigation_id)
        return _head_from_row(row) if row is not None else None

    async def get_version(self, plan_version_id: UUID) -> InvestigationPlanVersion | None:
        row = await self._session.get(InvestigationPlanVersionRow, plan_version_id)
        return _version_from_row(row) if row is not None else None

    async def get_by_version(
        self, investigation_id: UUID, version: int
    ) -> InvestigationPlanVersion | None:
        row = await self._session.scalar(
            select(InvestigationPlanVersionRow).where(
                InvestigationPlanVersionRow.investigation_id == investigation_id,
                InvestigationPlanVersionRow.version == version,
            )
        )
        return _version_from_row(row) if row is not None else None

    async def get_by_source(
        self, source_invocation_id: UUID, source_observation_id: UUID
    ) -> InvestigationPlanVersion | None:
        row = await self._session.scalar(
            select(InvestigationPlanVersionRow).where(
                InvestigationPlanVersionRow.source_invocation_id == source_invocation_id,
                InvestigationPlanVersionRow.source_observation_id == source_observation_id,
            )
        )
        return _version_from_row(row) if row is not None else None

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationPlanVersion, ...]:
        rows = await self._session.scalars(
            select(InvestigationPlanVersionRow)
            .where(InvestigationPlanVersionRow.investigation_id == investigation_id)
            .order_by(InvestigationPlanVersionRow.version)
        )
        return tuple(_version_from_row(row) for row in rows)

    async def create_version(
        self,
        version: InvestigationPlanVersion,
        *,
        expected_current_version: int | None,
    ) -> tuple[InvestigationPlanVersion, bool]:
        if expected_current_version is None:
            if version.version != 1:
                raise VersionConflictError("A first plan version must be v1")
            result = await self._session.execute(
                insert(InvestigationPlanRow)
                .values(
                    investigation_id=version.investigation_id,
                    current_version=version.version,
                    created_at=version.created_at,
                    updated_at=version.created_at,
                )
                .on_conflict_do_nothing(index_elements=[InvestigationPlanRow.investigation_id])
            )
            if int(cast(Any, result).rowcount or 0) != 1:
                raise VersionConflictError("Plan head was created concurrently")
        else:
            if version.version != expected_current_version + 1:
                raise VersionConflictError("Plan current version changed")
            result = await self._session.execute(
                update(InvestigationPlanRow)
                .where(
                    InvestigationPlanRow.investigation_id == version.investigation_id,
                    InvestigationPlanRow.current_version == expected_current_version,
                )
                .values(
                    current_version=version.version,
                    updated_at=version.created_at,
                )
            )
            if int(cast(Any, result).rowcount or 0) != 1:
                raise VersionConflictError("Plan current version changed")
        self._session.add(_version_row(version))
        await self._session.flush()
        return version, True

"""PostgreSQL repository for atomic subject-scoped Retrohunt slots."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.retrohunt import (
    RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT,
    RetrohuntReservation,
    RetrohuntReservationState,
)
from cti_app.infrastructure.database.models.retrohunt import RetrohuntReservationRow


class SqlAlchemyRetrohuntReservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve(
        self, reservation: RetrohuntReservation
    ) -> tuple[RetrohuntReservation, bool] | None:
        existing = await self._get_by_operation(reservation.subject_id, reservation.operation_key)
        if existing is not None:
            return existing, False

        for slot_number in range(1, RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT + 1):
            statement = (
                insert(RetrohuntReservationRow)
                .values(
                    id=reservation.id,
                    subject_id=reservation.subject_id,
                    investigation_id=reservation.investigation_id,
                    slot_number=slot_number,
                    operation_key=reservation.operation_key,
                    rule_version_id=reservation.rule_version_id,
                    rule_source_sha256=reservation.rule_source_sha256,
                    corpus_id=reservation.corpus_id,
                    corpus_sha256=reservation.corpus_sha256,
                    parameters=dict(reservation.parameters),
                    state=RetrohuntReservationState.RESERVED.value,
                    version=1,
                    created_at=reservation.created_at,
                    updated_at=reservation.updated_at,
                )
                .on_conflict_do_nothing()
                .returning(RetrohuntReservationRow.id)
            )
            inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
            if inserted_id is not None:
                row = await self._session.get(RetrohuntReservationRow, inserted_id)
                assert row is not None
                return _to_domain(row), True

            # ON CONFLICT also handles a concurrent replay of this operation.
            existing = await self._get_by_operation(
                reservation.subject_id, reservation.operation_key
            )
            if existing is not None:
                return existing, False
        return None

    async def get_for_subject(
        self, subject_id: UUID, reservation_id: UUID
    ) -> RetrohuntReservation | None:
        row = await self._session.scalar(
            select(RetrohuntReservationRow)
            .where(
                RetrohuntReservationRow.subject_id == subject_id,
                RetrohuntReservationRow.id == reservation_id,
            )
            .execution_options(populate_existing=True)
        )
        return _to_domain(row) if row is not None else None

    async def count_for_subject(self, subject_id: UUID) -> int:
        from sqlalchemy import func

        count = await self._session.scalar(
            select(func.count())
            .select_from(RetrohuntReservationRow)
            .where(RetrohuntReservationRow.subject_id == subject_id)
        )
        return int(count or 0)

    async def list_for_subject(self, subject_id: UUID) -> tuple[RetrohuntReservation, ...]:
        rows = await self._session.scalars(
            select(RetrohuntReservationRow)
            .where(RetrohuntReservationRow.subject_id == subject_id)
            .order_by(RetrohuntReservationRow.slot_number, RetrohuntReservationRow.created_at)
            .execution_options(populate_existing=True)
        )
        return tuple(_to_domain(row) for row in rows)

    async def update_if_version(
        self, reservation: RetrohuntReservation, *, expected_version: int
    ) -> RetrohuntReservation | None:
        statement = (
            update(RetrohuntReservationRow)
            .where(
                RetrohuntReservationRow.id == reservation.id,
                RetrohuntReservationRow.subject_id == reservation.subject_id,
                RetrohuntReservationRow.version == expected_version,
            )
            .values(
                state=reservation.state.value,
                submission_token=reservation.submission_token,
                provider_job_id=reservation.provider_job_id,
                version=reservation.version,
                updated_at=reservation.updated_at,
            )
            .returning(RetrohuntReservationRow.id)
        )
        updated_id = (await self._session.execute(statement)).scalar_one_or_none()
        if updated_id is None:
            return None
        row = await self._session.get(RetrohuntReservationRow, updated_id, populate_existing=True)
        assert row is not None
        return _to_domain(row)

    async def _get_by_operation(
        self, subject_id: UUID, operation_key: str
    ) -> RetrohuntReservation | None:
        row = await self._session.scalar(
            select(RetrohuntReservationRow)
            .where(
                RetrohuntReservationRow.subject_id == subject_id,
                RetrohuntReservationRow.operation_key == operation_key,
            )
            .execution_options(populate_existing=True)
        )
        return _to_domain(row) if row is not None else None


def _to_domain(row: RetrohuntReservationRow) -> RetrohuntReservation:
    return RetrohuntReservation(
        id=row.id,
        subject_id=row.subject_id,
        investigation_id=row.investigation_id,
        slot_number=row.slot_number,
        operation_key=row.operation_key,
        rule_version_id=row.rule_version_id,
        rule_source_sha256=row.rule_source_sha256,
        corpus_id=row.corpus_id,
        corpus_sha256=row.corpus_sha256,
        parameters=dict(row.parameters),
        state=RetrohuntReservationState(row.state),
        submission_token=row.submission_token,
        provider_job_id=row.provider_job_id,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = ["SqlAlchemyRetrohuntReservationRepository"]

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.infrastructure.database.models.editions import EditionAuditEventRow, EditionRow


class SqlAlchemyEditionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, edition: Edition) -> bool:
        statement = (
            insert(EditionRow)
            .values(**_edition_values(edition))
            .on_conflict_do_nothing(
                index_elements=[
                    EditionRow.country_code,
                    EditionRow.period_start,
                    EditionRow.period_end,
                ]
            )
            .returning(EditionRow.id)
        )
        inserted_id = await self._session.scalar(statement)
        if inserted_id is not None:
            return True

        existing_id = await self._session.scalar(
            select(EditionRow.id).where(
                EditionRow.country_code == edition.country_code,
                EditionRow.period_start == edition.period_start,
                EditionRow.period_end == edition.period_end,
            )
        )
        if existing_id is not None:
            edition.id = existing_id
        return False

    async def get(self, edition_id: UUID) -> Edition | None:
        row = await self._session.get(EditionRow, edition_id)
        return _edition_from_row(row) if row else None

    async def get_for_update(self, edition_id: UUID) -> Edition | None:
        row = await self._session.scalar(
            select(EditionRow).where(EditionRow.id == edition_id).with_for_update()
        )
        return _edition_from_row(row) if row else None

    async def get_by_logical_key(
        self, country_code: str, period_start: date, period_end: date
    ) -> Edition | None:
        row = await self._session.scalar(
            select(EditionRow).where(
                EditionRow.country_code == country_code,
                EditionRow.period_start == period_start,
                EditionRow.period_end == period_end,
            )
        )
        return _edition_from_row(row) if row else None

    async def update(self, edition: Edition, expected_version: int) -> bool:
        result = await self._session.execute(
            update(EditionRow)
            .where(EditionRow.id == edition.id, EditionRow.version == expected_version)
            .values(**_edition_values(edition))
            .returning(EditionRow.id)
        )
        return result.scalar_one_or_none() is not None

    async def list(
        self,
        *,
        offset: int,
        limit: int,
        country_code: str | None,
        period_start: date | None,
        period_end: date | None,
        status: EditionStatus | None,
    ) -> tuple[Sequence[Edition], int]:
        filters = []
        if country_code:
            filters.append(EditionRow.country_code == country_code)
        if period_start:
            filters.append(EditionRow.period_start >= period_start)
        if period_end:
            filters.append(EditionRow.period_end <= period_end)
        if status:
            filters.append(EditionRow.status == status.value)
        total = int(
            await self._session.scalar(select(func.count()).select_from(EditionRow).where(*filters))
            or 0
        )
        rows = await self._session.scalars(
            select(EditionRow)
            .where(*filters)
            .order_by(EditionRow.period_start.desc(), EditionRow.country_code, EditionRow.id)
            .offset(offset)
            .limit(limit)
        )
        return ([_edition_from_row(row) for row in rows], total)


class SqlAlchemyEditionAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: EditionAuditEvent) -> None:
        self._session.add(
            EditionAuditEventRow(
                id=event.id,
                edition_id=event.edition_id,
                actor_id=event.actor_id,
                action=event.action,
                before=event.before,
                after=event.after,
                correlation_id=event.correlation_id,
                occurred_at=event.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditionAuditEvent]:
        rows = await self._session.scalars(
            select(EditionAuditEventRow)
            .where(EditionAuditEventRow.edition_id == edition_id)
            .order_by(EditionAuditEventRow.occurred_at, EditionAuditEventRow.id)
        )
        return [_edition_audit_from_row(row) for row in rows]

def _edition_values(edition: Edition) -> dict[str, object]:
    return {
        "id": edition.id,
        "country": edition.country,
        "country_code": edition.country_code,
        "period_start": edition.period_start,
        "period_end": edition.period_end,
        "tlp": edition.tlp.value,
        "languages": list(edition.languages),
        "target_articles": edition.target_articles,
        "previous_edition_id": edition.previous_edition_id,
        "source_profile": edition.source_profile,
        "status": edition.status.value,
        "version": edition.version,
        "created_at": edition.created_at,
        "updated_at": edition.updated_at,
    }


def _edition_from_row(row: EditionRow) -> Edition:
    return Edition(
        id=row.id,
        country=row.country,
        country_code=row.country_code,
        period_start=row.period_start,
        period_end=row.period_end,
        tlp=TLP(row.tlp),
        languages=tuple(row.languages),
        target_articles=row.target_articles,
        previous_edition_id=row.previous_edition_id,
        source_profile=row.source_profile,
        status=EditionStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _edition_audit_from_row(row: EditionAuditEventRow) -> EditionAuditEvent:
    return EditionAuditEvent(
        id=row.id,
        edition_id=row.edition_id,
        actor_id=row.actor_id,
        action=row.action,
        before=row.before,
        after=row.after,
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
    )

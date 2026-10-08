"""Persistence adapter for the append-only source-addition journal."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.production_source_additions import (
    SourceAdditionJournalEntryV1,
    promotion_plan_from_json,
    promotion_plan_to_json,
    source_addition_from_json,
    source_addition_source_key,
    source_addition_to_json,
)
from cti_app.infrastructure.database.models.production import ProductionSourceAdditionRow


class SqlAlchemyProductionSourceAdditionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest_for_identity(
        self, production_run_id: UUID, request_id: UUID, source_key: str
    ) -> SourceAdditionJournalEntryV1 | None:
        row = await self._session.scalar(
            select(ProductionSourceAdditionRow)
            .where(
                ProductionSourceAdditionRow.production_run_id == production_run_id,
                ProductionSourceAdditionRow.request_id == request_id,
                ProductionSourceAdditionRow.source_key == source_key,
            )
            .order_by(ProductionSourceAdditionRow.version.desc())
            .limit(1)
        )
        return _entry_from_row(row) if row is not None else None

    async def list_latest_for_request(
        self, production_run_id: UUID, request_id: UUID
    ) -> Sequence[SourceAdditionJournalEntryV1]:
        ranked = (
            select(
                ProductionSourceAdditionRow.id.label("entry_id"),
                func.row_number()
                .over(
                    partition_by=ProductionSourceAdditionRow.source_key,
                    order_by=ProductionSourceAdditionRow.version.desc(),
                )
                .label("entry_rank"),
            )
            .where(
                ProductionSourceAdditionRow.production_run_id == production_run_id,
                ProductionSourceAdditionRow.request_id == request_id,
            )
            .subquery("latest_source_additions")
        )
        rows = await self._session.scalars(
            select(ProductionSourceAdditionRow)
            .join(ranked, ranked.c.entry_id == ProductionSourceAdditionRow.id)
            .where(ranked.c.entry_rank == 1)
            .order_by(ProductionSourceAdditionRow.source_key)
        )
        return [_entry_from_row(row) for row in rows]

    async def append(self, entry: SourceAdditionJournalEntryV1) -> None:
        addition = entry.addition
        source_key = source_addition_source_key(addition.canonical_url)
        row_id = uuid5(
            NAMESPACE_URL,
            f"production-source-addition:{addition.addition_id}:v{entry.version}",
        )
        self._session.add(
            ProductionSourceAdditionRow(
                id=row_id,
                addition_id=addition.addition_id,
                request_id=addition.request_id,
                production_run_id=addition.production_run_id,
                subject_id=addition.subject_id,
                source_key=source_key,
                version=entry.version,
                state=addition.state.value,
                origin=addition.origin.value,
                record=source_addition_to_json(addition),
                projection=promotion_plan_to_json(entry.promotion_plan),
                created_at=datetime.now(UTC),
            )
        )
        await self._session.flush()


def _entry_from_row(row: ProductionSourceAdditionRow) -> SourceAdditionJournalEntryV1:
    addition = source_addition_from_json(row.record)
    if (
        addition.addition_id != row.addition_id
        or addition.request_id != row.request_id
        or addition.production_run_id != row.production_run_id
        or addition.subject_id != row.subject_id
        or addition.state.value != row.state
        or addition.origin.value != row.origin
        or source_addition_source_key(addition.canonical_url) != row.source_key
    ):
        raise ValueError("source_addition_journal_identity_mismatch")
    return SourceAdditionJournalEntryV1(
        addition=addition,
        version=row.version,
        promotion_plan=promotion_plan_from_json(row.projection),
    )

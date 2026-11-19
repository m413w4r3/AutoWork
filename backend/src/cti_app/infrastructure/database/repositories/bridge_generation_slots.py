from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.editorial_bridge_generation_slot import (
    BridgeGenerationPreviousOutcome,
    BridgeGenerationSlot,
    BridgeGenerationSlotAcquireResult,
    BridgeGenerationSlotState,
)
from cti_app.domain.editorial_thread import EditorialThreadReconciliationOutcome
from cti_app.infrastructure.database.models.bridge_generation_slots import BridgeGenerationSlotRow


class SqlAlchemyBridgeGenerationSlotRepository:
    """Lock and update the singleton after the owning thread/job locks."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> BridgeGenerationSlot:
        row = await self._session.get(BridgeGenerationSlotRow, 1)
        if row is None:
            raise RuntimeError("bridge_generation_slot_missing")
        return _slot_from_row(row)

    async def is_held_unknown_for(self, *, lease_token: int, thread_id: UUID, job_id: UUID) -> bool:
        row = await self._locked_row()
        return (
            row.state == BridgeGenerationSlotState.HELD_UNKNOWN.value
            and row.lease_token == lease_token
            and row.holder_thread_id == thread_id
            and row.holder_job_id == job_id
        )

    async def is_held_for(self, *, lease_token: int, thread_id: UUID, job_id: UUID) -> bool:
        row = await self._locked_row()
        return (
            row.state == BridgeGenerationSlotState.HELD.value
            and row.lease_token == lease_token
            and row.holder_thread_id == thread_id
            and row.holder_job_id == job_id
        )

    async def try_acquire(
        self,
        *,
        expected_token: int,
        previous_outcome: BridgeGenerationPreviousOutcome,
        holder_thread_id: UUID,
        holder_job_id: UUID,
        lease_duration: timedelta,
        allow_reentry: bool,
        test_now: datetime | None = None,
    ) -> BridgeGenerationSlotAcquireResult:
        row = await self._session.scalar(
            select(BridgeGenerationSlotRow)
            .where(BridgeGenerationSlotRow.singleton_key == 1)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("bridge_generation_slot_missing")
        if row.lease_token != expected_token:
            return BridgeGenerationSlotAcquireResult.STALE

        state = BridgeGenerationSlotState(row.state)
        same_holder = (
            row.holder_thread_id == holder_thread_id and row.holder_job_id == holder_job_id
        )
        if (
            same_holder
            and state
            in {
                BridgeGenerationSlotState.HELD,
                BridgeGenerationSlotState.HELD_UNKNOWN,
            }
            and allow_reentry
        ):
            database_now = test_now or await self._database_now()
            row.state = BridgeGenerationSlotState.HELD.value
            row.lease_expires_at = database_now + lease_duration
            await self._session.flush()
            return BridgeGenerationSlotAcquireResult.REENTERED
        if state is BridgeGenerationSlotState.HELD_UNKNOWN:
            return BridgeGenerationSlotAcquireResult.HELD_UNKNOWN
        database_now = test_now or await self._database_now()
        if state is BridgeGenerationSlotState.HELD:
            if row.lease_expires_at is None or row.lease_expires_at > database_now:
                return BridgeGenerationSlotAcquireResult.BUSY
            if previous_outcome is BridgeGenerationPreviousOutcome.UNKNOWN:
                row.state = BridgeGenerationSlotState.HELD_UNKNOWN.value
                await self._session.flush()
                return BridgeGenerationSlotAcquireResult.HELD_UNKNOWN

        row.holder_thread_id = holder_thread_id
        row.holder_job_id = holder_job_id
        row.holder_turn_id = None
        row.lease_token += 1
        row.acquired_at = database_now
        row.lease_expires_at = database_now + lease_duration
        row.state = BridgeGenerationSlotState.HELD.value
        await self._session.flush()
        return BridgeGenerationSlotAcquireResult.ACQUIRED

    async def mark_unknown(
        self,
        *,
        lease_token: int,
        thread_id: UUID,
        job_id: UUID,
        turn_id: UUID | None,
    ) -> bool:
        row = await self._locked_row()
        if not _matches_holder(row, lease_token, thread_id, job_id, turn_id):
            return False
        row.state = BridgeGenerationSlotState.HELD_UNKNOWN.value
        if turn_id is not None:
            row.holder_turn_id = turn_id
        await self._session.flush()
        return True

    async def release_after_terminal(
        self,
        *,
        lease_token: int,
        thread_id: UUID,
        job_id: UUID,
        turn_id: UUID | None,
    ) -> bool:
        row = await self._locked_row()
        if not _matches_holder(row, lease_token, thread_id, job_id, turn_id):
            return False
        _clear_holder(row)
        await self._session.flush()
        return True

    async def release_after_reconciliation(
        self,
        *,
        lease_token: int,
        thread_id: UUID,
        job_id: UUID,
        turn_id: UUID | None,
        outcome: EditorialThreadReconciliationOutcome,
    ) -> bool:
        row = await self._locked_row()
        if (
            outcome
            not in {
                EditorialThreadReconciliationOutcome.RECONCILED_REPLY,
                EditorialThreadReconciliationOutcome.NOT_SUBMITTED,
                EditorialThreadReconciliationOutcome.SESSION_LOST,
            }
            or row.lease_token != lease_token
            or row.state
            not in {
                BridgeGenerationSlotState.HELD.value,
                BridgeGenerationSlotState.HELD_UNKNOWN.value,
            }
            or row.holder_thread_id != thread_id
            or row.holder_job_id != job_id
            or row.holder_turn_id not in (None, turn_id)
        ):
            return False
        _clear_holder(row)
        await self._session.flush()
        return True

    async def _database_now(self) -> datetime:
        now = await self._session.scalar(select(func.statement_timestamp()))
        if now is None:
            raise RuntimeError("database_clock_unavailable")
        return cast(datetime, now)

    async def _locked_row(self) -> BridgeGenerationSlotRow:
        row = await self._session.scalar(
            select(BridgeGenerationSlotRow)
            .where(BridgeGenerationSlotRow.singleton_key == 1)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("bridge_generation_slot_missing")
        return row


def _slot_from_row(row: BridgeGenerationSlotRow) -> BridgeGenerationSlot:
    return BridgeGenerationSlot(
        singleton_key=row.singleton_key,
        holder_thread_id=row.holder_thread_id,
        holder_job_id=row.holder_job_id,
        holder_turn_id=row.holder_turn_id,
        lease_token=row.lease_token,
        acquired_at=row.acquired_at,
        lease_expires_at=row.lease_expires_at,
        state=BridgeGenerationSlotState(row.state),
    )


def _matches_holder(
    row: BridgeGenerationSlotRow,
    lease_token: int,
    thread_id: UUID,
    job_id: UUID,
    turn_id: UUID | None,
) -> bool:
    return (
        row.lease_token == lease_token
        and row.state
        in {BridgeGenerationSlotState.HELD.value, BridgeGenerationSlotState.HELD_UNKNOWN.value}
        and row.holder_thread_id == thread_id
        and row.holder_job_id == job_id
        and row.holder_turn_id in (None, turn_id)
    )


def _clear_holder(row: BridgeGenerationSlotRow) -> None:
    row.holder_thread_id = None
    row.holder_job_id = None
    row.holder_turn_id = None
    row.acquired_at = None
    row.lease_expires_at = None
    row.state = BridgeGenerationSlotState.FREE.value

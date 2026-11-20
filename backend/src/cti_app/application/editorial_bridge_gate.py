"""Exclusive, fenced capacity gate for editorial discussion generations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from cti_app.application.editorial_conversation_turns import has_persisted_not_submitted_proof
from cti_app.application.editorial_thread_common import is_editorial_turn_objective
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.editorial_bridge_generation_slot import (
    BridgeGenerationPreviousOutcome,
    BridgeGenerationSlot,
    BridgeGenerationSlotAcquireResult,
    BridgeGenerationSlotState,
    EditorialBridgeGenerationLease,
)
from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
    EditorialThreadJobState,
)
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelRunStatus,
    ModelSubmissionState,
)

EDITORIAL_BRIDGE_SLOT_LEASE = timedelta(minutes=30)


class EditorialBridgeGateDisposition(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    CURRENT_REQUIRES_RECONCILIATION = "current_requires_reconciliation"


class EditorialBridgeSubmissionResolution(StrEnum):
    TERMINAL = "terminal"
    NOT_SUBMITTED = "not_submitted"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EditorialBridgeGateAttempt:
    disposition: EditorialBridgeGateDisposition
    lease: EditorialBridgeGenerationLease | None = None


class EditorialBridgeGate:
    """Acquire before ModelConversationService creates or submits a turn."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def acquire(
        self,
        *,
        thread_id: UUID,
        job_id: UUID,
        worker_lease_token: UUID,
        turn_idempotency_key: str,
        test_now: datetime | None = None,
    ) -> EditorialBridgeGateAttempt:
        for _ in range(3):
            async with self._uow_factory() as uow:
                claimed_job = await uow.editorial_threads.get_job_for_update(job_id)
                if (
                    claimed_job is None
                    or claimed_job.thread_id != thread_id
                    or not await uow.editorial_threads.is_current_worker_lease(
                        job_id=job_id,
                        thread_id=thread_id,
                        lease_token=worker_lease_token,
                    )
                ):
                    return EditorialBridgeGateAttempt(EditorialBridgeGateDisposition.BUSY)
            async with self._uow_factory() as uow:
                snapshot = await uow.bridge_generation_slots.get()
            previous_outcome = BridgeGenerationPreviousOutcome.UNKNOWN
            has_previous_turn = False
            same_holder = (
                snapshot.holder_thread_id == thread_id and snapshot.holder_job_id == job_id
            )
            if snapshot.state in {
                BridgeGenerationSlotState.HELD,
                BridgeGenerationSlotState.HELD_UNKNOWN,
            }:
                previous_outcome, has_previous_turn = await self._previous_attempt(snapshot)

            if (
                same_holder
                and has_previous_turn
                and previous_outcome is BridgeGenerationPreviousOutcome.UNKNOWN
            ):
                current_unknown = await self._mark_previous_reconciliation(
                    snapshot, has_previous_turn=True
                )
                if not current_unknown:
                    continue
                return EditorialBridgeGateAttempt(
                    EditorialBridgeGateDisposition.CURRENT_REQUIRES_RECONCILIATION
                )

            if snapshot.state is BridgeGenerationSlotState.HELD_UNKNOWN and not (
                same_holder and not has_previous_turn and snapshot.holder_turn_id is None
            ):
                current_unknown = await self._mark_previous_reconciliation(
                    snapshot, has_previous_turn=has_previous_turn
                )
                if not current_unknown:
                    continue
                if same_holder:
                    return EditorialBridgeGateAttempt(
                        EditorialBridgeGateDisposition.CURRENT_REQUIRES_RECONCILIATION
                    )
                return EditorialBridgeGateAttempt(EditorialBridgeGateDisposition.BUSY)

            async with self._uow_factory() as uow:
                # Lock order is editorial thread → its job → singleton slot.
                # No production-run lock is held or acquired on this path.
                current_job = await uow.editorial_threads.get_job_for_update(job_id)
                if current_job is None or current_job.thread_id != thread_id:
                    return EditorialBridgeGateAttempt(EditorialBridgeGateDisposition.BUSY)
                if not await uow.editorial_threads.is_current_worker_lease(
                    job_id=job_id,
                    thread_id=thread_id,
                    lease_token=worker_lease_token,
                ):
                    return EditorialBridgeGateAttempt(EditorialBridgeGateDisposition.BUSY)
                result = await uow.bridge_generation_slots.try_acquire(
                    expected_token=snapshot.lease_token,
                    previous_outcome=previous_outcome,
                    holder_thread_id=thread_id,
                    holder_job_id=job_id,
                    lease_duration=EDITORIAL_BRIDGE_SLOT_LEASE,
                    allow_reentry=(
                        same_holder and not has_previous_turn and snapshot.holder_turn_id is None
                    ),
                    test_now=test_now,
                )
                await uow.commit()

            if result is BridgeGenerationSlotAcquireResult.STALE:
                continue
            if result is BridgeGenerationSlotAcquireResult.ACQUIRED:
                return EditorialBridgeGateAttempt(
                    EditorialBridgeGateDisposition.ACQUIRED,
                    EditorialBridgeGenerationLease(
                        thread_id=thread_id,
                        job_id=job_id,
                        worker_lease_token=worker_lease_token,
                        turn_idempotency_key=turn_idempotency_key,
                        lease_token=snapshot.lease_token + 1,
                    ),
                )
            if result is BridgeGenerationSlotAcquireResult.REENTERED:
                return EditorialBridgeGateAttempt(
                    EditorialBridgeGateDisposition.ACQUIRED,
                    EditorialBridgeGenerationLease(
                        thread_id=thread_id,
                        job_id=job_id,
                        worker_lease_token=worker_lease_token,
                        turn_idempotency_key=turn_idempotency_key,
                        lease_token=snapshot.lease_token,
                    ),
                )
            if result is BridgeGenerationSlotAcquireResult.HELD_UNKNOWN:
                current_unknown = await self._mark_previous_reconciliation(
                    snapshot, has_previous_turn=has_previous_turn
                )
                if not current_unknown:
                    continue
                if same_holder:
                    return EditorialBridgeGateAttempt(
                        EditorialBridgeGateDisposition.CURRENT_REQUIRES_RECONCILIATION
                    )
            return EditorialBridgeGateAttempt(EditorialBridgeGateDisposition.BUSY)

        return EditorialBridgeGateAttempt(EditorialBridgeGateDisposition.BUSY)

    async def is_current_worker(self, lease: EditorialBridgeGenerationLease) -> bool:
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job_for_update(lease.job_id)
            if (
                job is None
                or job.thread_id != lease.thread_id
                or not await uow.editorial_threads.is_current_worker_lease(
                    job_id=lease.job_id,
                    thread_id=lease.thread_id,
                    lease_token=lease.worker_lease_token,
                )
            ):
                return False
            return await uow.bridge_generation_slots.is_held_for(
                lease_token=lease.lease_token,
                thread_id=lease.thread_id,
                job_id=lease.job_id,
            )

    async def finish_turn(self, lease: EditorialBridgeGenerationLease, *, turn_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job_for_update(lease.job_id)
            record = await uow.editorial_threads.get(lease.thread_id)
            if (
                job is None
                or job.thread_id != lease.thread_id
                or not await uow.editorial_threads.is_current_worker_lease(
                    job_id=lease.job_id,
                    thread_id=lease.thread_id,
                    lease_token=lease.worker_lease_token,
                )
                or record is None
                or record.thread.model_conversation_id is None
            ):
                return False
            turn = await uow.model_conversation_turns.get(turn_id)
            run = await uow.model_runs.get(turn.model_run_id) if turn is not None else None
            if (
                turn is None
                or run is None
                or turn.conversation_id != record.thread.model_conversation_id
                or turn.idempotency_key != lease.turn_idempotency_key
                or not is_editorial_turn_objective(turn.objective)
                or run.backend is not ModelBackend.CHATGPT_BRIDGE
                or not (
                    has_persisted_not_submitted_proof(turn, run)
                    or _verified_terminal_run(run.status, run.submission_state)
                )
            ):
                await uow.bridge_generation_slots.mark_unknown(
                    lease_token=lease.lease_token,
                    thread_id=lease.thread_id,
                    job_id=lease.job_id,
                    turn_id=turn_id,
                )
                await uow.commit()
                return False
            released = await uow.bridge_generation_slots.release_after_terminal(
                lease_token=lease.lease_token,
                thread_id=lease.thread_id,
                job_id=lease.job_id,
                turn_id=turn_id,
            )
            await uow.commit()
            return released

    async def finish_failed_attempt(
        self,
        lease: EditorialBridgeGenerationLease,
        *,
        turn_idempotency_key: str,
    ) -> EditorialBridgeSubmissionResolution:
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job_for_update(lease.job_id)
            if (
                job is None
                or job.thread_id != lease.thread_id
                or not await uow.editorial_threads.is_current_worker_lease(
                    job_id=lease.job_id,
                    thread_id=lease.thread_id,
                    lease_token=lease.worker_lease_token,
                )
            ):
                return EditorialBridgeSubmissionResolution.UNKNOWN
            turn = await uow.model_conversation_turns.get_by_idempotency_key(turn_idempotency_key)
            record = await uow.editorial_threads.get(lease.thread_id)
            run = await uow.model_runs.get(turn.model_run_id) if turn is not None else None
            exact_turn = (
                turn is not None
                and record is not None
                and turn.conversation_id == record.thread.model_conversation_id
                and is_editorial_turn_objective(turn.objective)
                and run is not None
                and run.backend is ModelBackend.CHATGPT_BRIDGE
            )
            if turn is None:
                # A missing write is not durable proof that no submit occurred.
                resolution = EditorialBridgeSubmissionResolution.UNKNOWN
                turn_id = None
            elif exact_turn and run is not None and has_persisted_not_submitted_proof(turn, run):
                resolution = EditorialBridgeSubmissionResolution.NOT_SUBMITTED
                turn_id = turn.id
            elif (
                exact_turn
                and run is not None
                and _verified_terminal_run(run.status, run.submission_state)
            ):
                resolution = EditorialBridgeSubmissionResolution.TERMINAL
                turn_id = turn.id
            else:
                resolution = EditorialBridgeSubmissionResolution.UNKNOWN
                turn_id = turn.id

            if resolution is EditorialBridgeSubmissionResolution.UNKNOWN:
                await uow.bridge_generation_slots.mark_unknown(
                    lease_token=lease.lease_token,
                    thread_id=lease.thread_id,
                    job_id=lease.job_id,
                    turn_id=turn_id,
                )
            else:
                await uow.bridge_generation_slots.release_after_terminal(
                    lease_token=lease.lease_token,
                    thread_id=lease.thread_id,
                    job_id=lease.job_id,
                    turn_id=turn_id,
                )
            await uow.commit()
            return resolution

    async def _previous_outcome(
        self, slot: BridgeGenerationSlot
    ) -> BridgeGenerationPreviousOutcome:
        outcome, _has_turn = await self._previous_attempt(slot)
        return outcome

    async def _previous_attempt(
        self, slot: BridgeGenerationSlot
    ) -> tuple[BridgeGenerationPreviousOutcome, bool]:
        if slot.holder_thread_id is None or slot.holder_job_id is None:
            return BridgeGenerationPreviousOutcome.UNKNOWN, False
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job(slot.holder_job_id)
            record = await uow.editorial_threads.get(slot.holder_thread_id)
            if (
                job is None
                or job.thread_id != slot.holder_thread_id
                or record is None
                or record.thread.model_conversation_id is None
            ):
                return BridgeGenerationPreviousOutcome.UNKNOWN, False
            idempotency_key = f"editorial-thread-message:{job.send_event_id}"
            turn = (
                await uow.model_conversation_turns.get(slot.holder_turn_id)
                if slot.holder_turn_id is not None
                else await uow.model_conversation_turns.get_by_idempotency_key(idempotency_key)
            )
            if turn is None:
                # Missing persisted state is not proof that an external submit did not occur.
                return (
                    BridgeGenerationPreviousOutcome.UNKNOWN,
                    slot.holder_turn_id is not None,
                )
            if (
                turn.conversation_id != record.thread.model_conversation_id
                or turn.idempotency_key != idempotency_key
                or not is_editorial_turn_objective(turn.objective)
            ):
                return BridgeGenerationPreviousOutcome.UNKNOWN, True
            run = await uow.model_runs.get(turn.model_run_id)
            if run is None or run.backend is not ModelBackend.CHATGPT_BRIDGE:
                return BridgeGenerationPreviousOutcome.UNKNOWN, True
            if has_persisted_not_submitted_proof(turn, run):
                return BridgeGenerationPreviousOutcome.NOT_SUBMITTED, True
            if _verified_terminal_run(run.status, run.submission_state):
                return BridgeGenerationPreviousOutcome.TERMINAL, True
            return BridgeGenerationPreviousOutcome.UNKNOWN, True

    async def _mark_previous_reconciliation(
        self, slot: BridgeGenerationSlot, *, has_previous_turn: bool
    ) -> bool:
        if slot.holder_thread_id is None or slot.holder_job_id is None:
            return False
        async with self._uow_factory() as uow:
            # Keep thread → job → slot order and verify the holder did not reconcile
            # between the initial snapshot and this transaction.
            job = await uow.editorial_threads.get_job_for_update(slot.holder_job_id)
            if job is None or job.thread_id != slot.holder_thread_id:
                return False
            still_unknown = await uow.bridge_generation_slots.is_held_unknown_for(
                lease_token=slot.lease_token,
                thread_id=slot.holder_thread_id,
                job_id=slot.holder_job_id,
            )
            if not still_unknown:
                return False
            if job.state is EditorialThreadJobState.COMPLETE:
                return False
            if has_previous_turn:
                await uow.editorial_threads.set_job_state(
                    job.id,
                    EditorialThreadJobState.RECONCILIATION_REQUIRED,
                    lease_token=job.lease_token,
                )
            await uow.editorial_threads.set_bridge_status(
                job.thread_id, EditorialBridgeStatus.RECONCILIATION_REQUIRED
            )
            await uow.commit()
            return True


def _verified_terminal_run(status: ModelRunStatus, submission_state: ModelSubmissionState) -> bool:
    return (
        (
            status is ModelRunStatus.SUCCEEDED
            and submission_state is ModelSubmissionState.RESULT_OBTAINED
        )
        or (
            status is ModelRunStatus.FAILED
            and submission_state is ModelSubmissionState.VERIFIED_TERMINAL_FAILURE
        )
        or (
            status is ModelRunStatus.BLOCKED
            and submission_state is ModelSubmissionState.NOT_SUBMITTED
        )
    )

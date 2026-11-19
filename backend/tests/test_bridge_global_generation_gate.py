from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.editorial_bridge_gate import EditorialBridgeGate
from cti_app.domain.editorial_bridge_generation_slot import (
    BridgeGenerationPreviousOutcome,
    BridgeGenerationSlot,
    BridgeGenerationSlotAcquireResult,
    BridgeGenerationSlotState,
)
from cti_app.domain.editorial_thread import (
    EditorialThreadJob,
    EditorialThreadJobState,
    EditorialThreadReconciliationOutcome,
)
from cti_app.domain.model_conversations import ConversationTurnStatus
from cti_app.domain.model_runs import ModelBackend, ModelRunStatus, ModelSubmissionState


class MemoryBridgeGenerationSlots:
    """PostgreSQL-less shared slot used by independent worker instances."""

    def __init__(self) -> None:
        self.row = BridgeGenerationSlot(
            singleton_key=1,
            holder_thread_id=None,
            holder_job_id=None,
            holder_turn_id=None,
            lease_token=0,
            acquired_at=None,
            lease_expires_at=None,
            state=BridgeGenerationSlotState.FREE,
        )
        self.lock = asyncio.Lock()

    async def get(self):
        return self.row

    async def is_held_unknown_for(self, *, lease_token, thread_id, job_id):
        async with self.lock:
            return (
                self.row.state is BridgeGenerationSlotState.HELD_UNKNOWN
                and self.row.lease_token == lease_token
                and self.row.holder_thread_id == thread_id
                and self.row.holder_job_id == job_id
            )

    async def try_acquire(self, **values):
        async with self.lock:
            row = self.row
            now = values["test_now"] or datetime.now(UTC)
            if row.lease_token != values["expected_token"]:
                return BridgeGenerationSlotAcquireResult.STALE
            same_holder = (
                row.holder_thread_id == values["holder_thread_id"]
                and row.holder_job_id == values["holder_job_id"]
            )
            if (
                same_holder
                and row.state
                in {BridgeGenerationSlotState.HELD, BridgeGenerationSlotState.HELD_UNKNOWN}
                and values["allow_reentry"]
            ):
                self.row = replace(
                    row,
                    state=BridgeGenerationSlotState.HELD,
                    lease_expires_at=now + values["lease_duration"],
                )
                return BridgeGenerationSlotAcquireResult.REENTERED
            if row.state is BridgeGenerationSlotState.FREE:
                pass
            elif row.lease_expires_at is not None and row.lease_expires_at <= now:
                if values["previous_outcome"] is BridgeGenerationPreviousOutcome.UNKNOWN:
                    self.row = replace(row, state=BridgeGenerationSlotState.HELD_UNKNOWN)
                    return BridgeGenerationSlotAcquireResult.HELD_UNKNOWN
            else:
                return BridgeGenerationSlotAcquireResult.BUSY
            self.row = BridgeGenerationSlot(
                singleton_key=1,
                holder_thread_id=values["holder_thread_id"],
                holder_job_id=values["holder_job_id"],
                holder_turn_id=None,
                lease_token=row.lease_token + 1,
                acquired_at=now,
                lease_expires_at=now + values["lease_duration"],
                state=BridgeGenerationSlotState.HELD,
            )
            return BridgeGenerationSlotAcquireResult.ACQUIRED

    async def is_held_for(self, *, lease_token, thread_id, job_id):
        return (
            self.row.state is BridgeGenerationSlotState.HELD
            and self.row.lease_token == lease_token
            and self.row.holder_thread_id == thread_id
            and self.row.holder_job_id == job_id
        )

    async def set_turn(self, *, lease_token, job_id, turn_id):
        async with self.lock:
            if self.row.lease_token != lease_token or self.row.holder_job_id != job_id:
                return False
            self.row = replace(self.row, holder_turn_id=turn_id)
            return True

    async def release(self, *, lease_token, job_id, turn_id):
        async with self.lock:
            if self.row.lease_token != lease_token or self.row.holder_job_id != job_id:
                return False
            if self.row.holder_turn_id not in (None, turn_id):
                return False
            self.row = BridgeGenerationSlot(
                singleton_key=1,
                holder_thread_id=None,
                holder_job_id=None,
                holder_turn_id=None,
                lease_token=self.row.lease_token,
                acquired_at=None,
                lease_expires_at=None,
                state=BridgeGenerationSlotState.FREE,
            )
            return True

    async def mark_unknown(self, *, lease_token, thread_id, job_id, turn_id):
        async with self.lock:
            if (
                self.row.lease_token != lease_token
                or self.row.holder_thread_id != thread_id
                or self.row.holder_job_id != job_id
            ):
                return False
            self.row = replace(
                self.row,
                state=BridgeGenerationSlotState.HELD_UNKNOWN,
                holder_turn_id=turn_id or self.row.holder_turn_id,
            )
            return True

    async def release_after_terminal(self, *, lease_token, thread_id, job_id, turn_id):
        if self.row.lease_token != lease_token or self.row.holder_thread_id != thread_id:
            return False
        return await self.release(lease_token=lease_token, job_id=job_id, turn_id=turn_id)

    async def release_after_reconciliation(
        self, *, lease_token, thread_id, job_id, turn_id, outcome
    ):
        if (
            outcome
            not in {
                EditorialThreadReconciliationOutcome.RECONCILED_REPLY,
                EditorialThreadReconciliationOutcome.NOT_SUBMITTED,
                EditorialThreadReconciliationOutcome.SESSION_LOST,
            }
            or self.row.lease_token != lease_token
            or self.row.state
            not in {BridgeGenerationSlotState.HELD, BridgeGenerationSlotState.HELD_UNKNOWN}
            or self.row.holder_thread_id != thread_id
            or self.row.holder_job_id != job_id
            or self.row.holder_turn_id not in (None, turn_id)
        ):
            return False
        self.row = BridgeGenerationSlot(
            singleton_key=1,
            holder_thread_id=None,
            holder_job_id=None,
            holder_turn_id=None,
            lease_token=self.row.lease_token,
            acquired_at=None,
            lease_expires_at=None,
            state=BridgeGenerationSlotState.FREE,
        )
        return True


class MemoryEditorialThreads:
    def __init__(self, jobs, records):
        self.jobs = jobs
        self.records = records
        self.statuses = {job.thread_id: "generating" for job in jobs.values()}

    async def get_job_for_update(self, job_id):
        return self.jobs.get(job_id)

    async def is_current_worker_lease(self, *, job_id, thread_id, lease_token):
        job = self.jobs.get(job_id)
        return bool(
            job is not None
            and job.thread_id == thread_id
            and job.state is EditorialThreadJobState.RUNNING
            and job.lease_token == lease_token
            and job.lease_until is not None
            and job.lease_until > datetime.now(UTC)
        )

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def get(self, thread_id):
        return self.records.get(thread_id)

    async def set_job_state(self, job_id, state, **_values):
        self.jobs[job_id] = replace(
            self.jobs[job_id],
            state=state,
            lease_token=None,
            lease_until=_values.get("lease_until"),
        )

    async def set_bridge_status(self, thread_id, status):
        self.statuses[thread_id] = status.value


class MemoryRuns:
    def __init__(self):
        self.values = {}

    async def get(self, _run_id):
        return self.values.get(_run_id)


class MemoryTurns:
    def __init__(self):
        self.values = {}

    async def get(self, _turn_id):
        return self.values.get(_turn_id)

    async def get_by_idempotency_key(self, _key):
        return next((turn for turn in self.values.values() if turn.idempotency_key == _key), None)


class MemoryUow:
    def __init__(self, threads, slots):
        self.editorial_threads = threads
        self.bridge_generation_slots = slots
        self.model_conversation_turns = MemoryTurns()
        self.model_runs = MemoryRuns()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None


def make_gate_harness():
    now = datetime.now(UTC)
    first_thread, second_thread = uuid4(), uuid4()
    first_job, second_job = uuid4(), uuid4()
    jobs = {
        first_job: EditorialThreadJob(
            id=first_job,
            thread_id=first_thread,
            send_event_id=uuid4(),
            state=EditorialThreadJobState.RUNNING,
            lease_until=now + timedelta(minutes=30),
            lease_token=uuid4(),
        ),
        second_job: EditorialThreadJob(
            id=second_job,
            thread_id=second_thread,
            send_event_id=uuid4(),
            state=EditorialThreadJobState.RUNNING,
            lease_until=now + timedelta(minutes=30),
            lease_token=uuid4(),
        ),
    }
    conversations = {first_thread: uuid4(), second_thread: uuid4()}
    records = {
        thread_id: SimpleNamespace(thread=SimpleNamespace(model_conversation_id=conversation_id))
        for thread_id, conversation_id in conversations.items()
    }
    slots = MemoryBridgeGenerationSlots()
    threads = MemoryEditorialThreads(jobs, records)
    uow = MemoryUow(threads, slots)
    return (
        now,
        (first_thread, second_thread),
        (first_job, second_job),
        conversations,
        slots,
        threads,
        uow,
        EditorialBridgeGate(lambda: uow),
    )


def add_model_attempt(uow, *, turn_id, conversation_id, key, status, submission_state):
    run_id = uuid4()
    uow.model_conversation_turns.values[turn_id] = SimpleNamespace(
        id=turn_id,
        conversation_id=conversation_id,
        idempotency_key=key,
        objective="editorial_message",
        model_run_id=run_id,
        status=ConversationTurnStatus.NEEDS_REVIEW,
        output_blob_reference=None,
        output_sha256=None,
    )
    uow.model_runs.values[run_id] = SimpleNamespace(
        backend=ModelBackend.CHATGPT_BRIDGE,
        status=status,
        submission_state=submission_state,
        response_id=None,
        output_references=(),
        raw_output_reference=None,
        raw_output_sha256=None,
        raw_output_chars=None,
    )
    return turn_id


def test_two_processes_never_submit_bridge_concurrently() -> None:
    """Two worker objects share only the database-less singleton slot."""
    now = datetime.now(UTC)
    first_thread, second_thread = uuid4(), uuid4()
    first_job, second_job = uuid4(), uuid4()
    jobs = {
        first_job: EditorialThreadJob(
            id=first_job,
            thread_id=first_thread,
            send_event_id=uuid4(),
            state=EditorialThreadJobState.RUNNING,
            lease_until=now + timedelta(minutes=30),
            lease_token=uuid4(),
        ),
        second_job: EditorialThreadJob(
            id=second_job,
            thread_id=second_thread,
            send_event_id=uuid4(),
            state=EditorialThreadJobState.RUNNING,
            lease_until=now + timedelta(minutes=30),
            lease_token=uuid4(),
        ),
    }
    slots = MemoryBridgeGenerationSlots()
    conversations = {first_thread: uuid4(), second_thread: uuid4()}
    records = {
        thread_id: SimpleNamespace(thread=SimpleNamespace(model_conversation_id=conversation_id))
        for thread_id, conversation_id in conversations.items()
    }
    threads = MemoryEditorialThreads(jobs, records)
    uow = MemoryUow(threads, slots)
    gates = (EditorialBridgeGate(lambda: uow), EditorialBridgeGate(lambda: uow))
    active = 0
    maximum_active = 0
    submitted_keys: list[str] = []
    release_first = asyncio.Event()
    first_entered = asyncio.Event()

    async def fake_gateway(key: str, turn_id, thread_id) -> None:
        nonlocal active, maximum_active
        submitted_keys.append(key)
        run_id = uuid4()
        conversation_id = conversations[thread_id]
        uow.model_conversation_turns.values[turn_id] = SimpleNamespace(
            id=turn_id,
            conversation_id=conversation_id,
            idempotency_key=key,
            objective="editorial_message",
            model_run_id=run_id,
            status=ConversationTurnStatus.SUCCEEDED,
        )
        uow.model_runs.values[run_id] = SimpleNamespace(
            backend=ModelBackend.CHATGPT_BRIDGE,
            status=ModelRunStatus.SUCCEEDED,
            submission_state=ModelSubmissionState.RESULT_OBTAINED,
        )
        active += 1
        maximum_active = max(maximum_active, active)
        if len(submitted_keys) == 1:
            first_entered.set()
            await release_first.wait()
        active -= 1

    async def submit(worker_index, thread_id, job_id, key):
        gate = gates[worker_index]
        attempt = await gate.acquire(
            thread_id=thread_id,
            job_id=job_id,
            worker_lease_token=threads.jobs[job_id].lease_token,
            turn_idempotency_key=key,
            test_now=now,
        )
        if attempt.lease is None:
            return False
        turn_id = uuid4()
        await fake_gateway(key, turn_id, thread_id)
        assert await gate.finish_turn(attempt.lease, turn_id=turn_id)
        return True

    async def oracle() -> None:
        first = asyncio.create_task(submit(0, first_thread, first_job, "editorial-key-01"))
        await first_entered.wait()
        second = await submit(1, second_thread, second_job, "editorial-key-02")
        assert second is False
        release_first.set()
        assert await first is True
        # Capacity retries reuse the already-persisted request key.
        assert await submit(1, second_thread, second_job, "editorial-key-02") is True

    asyncio.run(oracle())
    assert maximum_active == 1
    assert submitted_keys == ["editorial-key-01", "editorial-key-02"]


@pytest.mark.asyncio
async def test_unknown_external_outcome_retains_fencing_slot() -> None:
    (
        now,
        (first_thread, second_thread),
        (first_job, second_job),
        conversations,
        slots,
        threads,
        uow,
        gate,
    ) = make_gate_harness()
    key = f"editorial-thread-message:{threads.jobs[first_job].send_event_id}"
    first = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=threads.jobs[first_job].lease_token,
        turn_idempotency_key=key,
        test_now=now,
    )
    assert first.lease is not None
    turn_id = add_model_attempt(
        uow,
        turn_id=uuid4(),
        conversation_id=conversations[first_thread],
        key=key,
        status=ModelRunStatus.NEEDS_REVIEW,
        submission_state=ModelSubmissionState.EXTERNAL_STATE_UNKNOWN,
    )

    result = await gate.finish_failed_attempt(first.lease, turn_idempotency_key=key)
    assert result.value == "unknown"
    assert slots.row.state is BridgeGenerationSlotState.HELD_UNKNOWN
    assert slots.row.holder_turn_id == turn_id

    slots.row = replace(slots.row, lease_expires_at=now - timedelta(seconds=1))
    second = await gate.acquire(
        thread_id=second_thread,
        job_id=second_job,
        worker_lease_token=threads.jobs[second_job].lease_token,
        turn_idempotency_key="editorial-key-second",
        test_now=now,
    )
    assert second.lease is None
    assert slots.row.state is BridgeGenerationSlotState.HELD_UNKNOWN
    assert slots.row.lease_token == first.lease.lease_token
    assert threads.jobs[first_job].state is EditorialThreadJobState.RECONCILIATION_REQUIRED
    assert threads.statuses[first_thread] == "reconciliation_required"

    assert not await slots.release_after_reconciliation(
        lease_token=first.lease.lease_token,
        thread_id=first_thread,
        job_id=first_job,
        turn_id=turn_id,
        outcome=EditorialThreadReconciliationOutcome.STILL_UNKNOWN,
    )
    assert await slots.release_after_reconciliation(
        lease_token=first.lease.lease_token,
        thread_id=first_thread,
        job_id=first_job,
        turn_id=turn_id,
        outcome=EditorialThreadReconciliationOutcome.RECONCILED_REPLY,
    )
    assert slots.row.state is BridgeGenerationSlotState.FREE


@pytest.mark.asyncio
async def test_missing_turn_is_not_proof_that_submission_did_not_happen() -> None:
    (
        now,
        (first_thread, _second_thread),
        (first_job, _second_job),
        _conversations,
        slots,
        threads,
        _uow,
        gate,
    ) = make_gate_harness()
    key = f"editorial-thread-message:{threads.jobs[first_job].send_event_id}"
    first = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=threads.jobs[first_job].lease_token,
        turn_idempotency_key=key,
        test_now=now,
    )
    assert first.lease is not None

    resolution = await gate.finish_failed_attempt(first.lease, turn_idempotency_key=key)

    assert resolution.value == "unknown"
    assert slots.row.state is BridgeGenerationSlotState.HELD_UNKNOWN
    assert slots.row.holder_job_id == first_job
    assert slots.row.lease_token == first.lease.lease_token


@pytest.mark.asyncio
async def test_same_job_reenters_expired_slot_without_advancing_fence() -> None:
    (
        now,
        (first_thread, _second_thread),
        (first_job, _second_job),
        _conversations,
        slots,
        threads,
        _uow,
        gate,
    ) = make_gate_harness()
    key = f"editorial-thread-message:{threads.jobs[first_job].send_event_id}"
    original = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=threads.jobs[first_job].lease_token,
        turn_idempotency_key=key,
        test_now=now,
    )
    assert original.lease is not None
    slots.row = replace(slots.row, lease_expires_at=now - timedelta(seconds=1))
    reclaimed_worker_token = uuid4()
    threads.jobs[first_job] = replace(
        threads.jobs[first_job],
        lease_token=reclaimed_worker_token,
        lease_until=now + timedelta(minutes=30),
    )

    recovered = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=reclaimed_worker_token,
        turn_idempotency_key=key,
        test_now=now,
    )

    assert recovered.lease is not None
    assert recovered.lease.lease_token == original.lease.lease_token
    assert slots.row.state is BridgeGenerationSlotState.HELD
    assert slots.row.holder_job_id == first_job


@pytest.mark.asyncio
async def test_expired_lease_takeover_requires_terminal_or_not_submitted_proof() -> None:
    (
        now,
        (first_thread, second_thread),
        (first_job, second_job),
        conversations,
        slots,
        threads,
        uow,
        gate,
    ) = make_gate_harness()
    first_key = f"editorial-thread-message:{threads.jobs[first_job].send_event_id}"
    first = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=threads.jobs[first_job].lease_token,
        turn_idempotency_key=first_key,
        test_now=now,
    )
    assert first.lease is not None

    turn_id = add_model_attempt(
        uow,
        turn_id=uuid4(),
        conversation_id=conversations[first_thread],
        key=first_key,
        status=ModelRunStatus.SUCCEEDED,
        submission_state=ModelSubmissionState.RESULT_OBTAINED,
    )
    slots.row = replace(slots.row, lease_expires_at=now - timedelta(seconds=1))
    second = await gate.acquire(
        thread_id=second_thread,
        job_id=second_job,
        worker_lease_token=threads.jobs[second_job].lease_token,
        turn_idempotency_key="editorial-key-second",
        test_now=now,
    )
    assert second.lease is not None
    assert second.lease.lease_token == first.lease.lease_token + 1

    # The old holder may finish local bookkeeping, but its fence cannot release the new lease.
    assert not await gate.finish_turn(first.lease, turn_id=turn_id)
    assert slots.row.holder_job_id == second_job
    assert slots.row.lease_token == second.lease.lease_token


@pytest.mark.asyncio
async def test_expired_lease_takeover_accepts_persisted_not_submitted_proof() -> None:
    (
        now,
        (first_thread, second_thread),
        (first_job, second_job),
        conversations,
        slots,
        threads,
        uow,
        gate,
    ) = make_gate_harness()
    first_key = f"editorial-thread-message:{threads.jobs[first_job].send_event_id}"
    first = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=threads.jobs[first_job].lease_token,
        turn_idempotency_key=first_key,
        test_now=now,
    )
    assert first.lease is not None
    add_model_attempt(
        uow,
        turn_id=uuid4(),
        conversation_id=conversations[first_thread],
        key=first_key,
        status=ModelRunStatus.FAILED,
        submission_state=ModelSubmissionState.NOT_SUBMITTED,
    )
    slots.row = replace(slots.row, lease_expires_at=now - timedelta(seconds=1))

    takeover = await gate.acquire(
        thread_id=second_thread,
        job_id=second_job,
        worker_lease_token=threads.jobs[second_job].lease_token,
        turn_idempotency_key="editorial-key-second",
        test_now=now,
    )
    assert takeover.lease is not None
    assert takeover.lease.lease_token == first.lease.lease_token + 1


@pytest.mark.asyncio
async def test_expired_lease_without_persisted_proof_is_held_unknown() -> None:
    (
        now,
        (first_thread, second_thread),
        (first_job, second_job),
        _conversations,
        slots,
        threads,
        _uow,
        gate,
    ) = make_gate_harness()
    first_key = f"editorial-thread-message:{threads.jobs[first_job].send_event_id}"
    first = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=threads.jobs[first_job].lease_token,
        turn_idempotency_key=first_key,
        test_now=now,
    )
    assert first.lease is not None
    slots.row = replace(slots.row, lease_expires_at=now - timedelta(seconds=1))

    takeover = await gate.acquire(
        thread_id=second_thread,
        job_id=second_job,
        worker_lease_token=threads.jobs[second_job].lease_token,
        turn_idempotency_key="editorial-key-second",
        test_now=now,
    )
    assert takeover.lease is None
    assert slots.row.state is BridgeGenerationSlotState.HELD_UNKNOWN
    assert slots.row.holder_job_id == first_job
    assert slots.row.lease_token == first.lease.lease_token
    assert threads.jobs[first_job].state is EditorialThreadJobState.RUNNING
    reclaimed_worker_token = uuid4()
    threads.jobs[first_job] = replace(
        threads.jobs[first_job],
        lease_token=reclaimed_worker_token,
        lease_until=now + timedelta(minutes=30),
    )
    recovered = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=reclaimed_worker_token,
        turn_idempotency_key=first_key,
        test_now=now,
    )
    assert recovered.lease is not None
    assert recovered.lease.lease_token == first.lease.lease_token


@pytest.mark.asyncio
async def test_stale_worker_lease_cannot_reacquire_the_slot() -> None:
    (
        now,
        (first_thread, _second_thread),
        (first_job, _second_job),
        _conversations,
        slots,
        threads,
        _uow,
        gate,
    ) = make_gate_harness()
    original_worker_token = threads.jobs[first_job].lease_token
    key = f"editorial-thread-message:{threads.jobs[first_job].send_event_id}"
    first = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=original_worker_token,
        turn_idempotency_key=key,
        test_now=now,
    )
    assert first.lease is not None
    threads.jobs[first_job] = replace(
        threads.jobs[first_job],
        lease_token=uuid4(),
        lease_until=now + timedelta(minutes=30),
    )

    stale = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=original_worker_token,
        turn_idempotency_key=key,
        test_now=now,
    )

    assert stale.lease is None
    assert slots.row.state is BridgeGenerationSlotState.HELD
    assert slots.row.lease_token == first.lease.lease_token


@pytest.mark.asyncio
async def test_submitted_before_release_crash_retains_unknown_fencing_slot() -> None:
    (
        now,
        (first_thread, second_thread),
        (first_job, second_job),
        conversations,
        slots,
        threads,
        uow,
        gate,
    ) = make_gate_harness()
    first_key = f"editorial-thread-message:{threads.jobs[first_job].send_event_id}"
    first = await gate.acquire(
        thread_id=first_thread,
        job_id=first_job,
        worker_lease_token=threads.jobs[first_job].lease_token,
        turn_idempotency_key=first_key,
        test_now=now,
    )
    assert first.lease is not None

    # The gateway submission may have happened, but this worker crashed before
    # recording a terminal result or releasing its lease.
    turn_id = add_model_attempt(
        uow,
        turn_id=uuid4(),
        conversation_id=conversations[first_thread],
        key=first_key,
        status=ModelRunStatus.NEEDS_REVIEW,
        submission_state=ModelSubmissionState.EXTERNAL_STATE_UNKNOWN,
    )
    slots.row = replace(
        slots.row,
        holder_turn_id=turn_id,
        lease_expires_at=now - timedelta(seconds=1),
    )

    takeover = await gate.acquire(
        thread_id=second_thread,
        job_id=second_job,
        worker_lease_token=threads.jobs[second_job].lease_token,
        turn_idempotency_key="editorial-key-second",
        test_now=now,
    )
    assert takeover.lease is None
    assert slots.row.state is BridgeGenerationSlotState.HELD_UNKNOWN
    assert slots.row.lease_token == first.lease.lease_token
    assert threads.jobs[first_job].state is EditorialThreadJobState.RECONCILIATION_REQUIRED

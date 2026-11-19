from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError

from cti_app.application.editorial_bridge_gate import (
    EditorialBridgeGate,
    EditorialBridgeGateDisposition,
)
from cti_app.domain.editorial_bridge_generation_slot import (
    BridgeGenerationPreviousOutcome,
    BridgeGenerationSlotAcquireResult,
    BridgeGenerationSlotState,
)
from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
    EditorialThreadJobState,
    EditorialThreadReconciliationOutcome,
)
from cti_app.infrastructure.database.models.bridge_generation_slots import BridgeGenerationSlotRow
from cti_app.infrastructure.database.models.editorial_threads import (
    EditorialThreadJobRow,
    EditorialThreadRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationRow,
    ModelConversationTurnRow,
    ModelRunRow,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.editorial.test_threads_postgres import _create_thread_context, _event

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def reset_bridge_generation_slot(migrated_postgres_url: str) -> AsyncIterator[None]:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    async def reset() -> None:
        async with session_factory() as session, session.begin():
            result = await session.execute(
                update(BridgeGenerationSlotRow)
                .where(BridgeGenerationSlotRow.singleton_key == 1)
                .values(
                    state=BridgeGenerationSlotState.FREE.value,
                    holder_thread_id=None,
                    holder_job_id=None,
                    holder_turn_id=None,
                    acquired_at=None,
                    lease_expires_at=None,
                    lease_token=0,
                )
            )
            assert result.rowcount == 1

    try:
        await reset()
        yield
        await reset()
    finally:
        await engine.dispose()


async def _create_job(session_factory) -> tuple[UUID, UUID]:
    thread = await _create_thread_context(session_factory)
    send_event = await _event(thread.id, 1, f"bridge-gate-{uuid4().hex}")
    job_id = uuid4()
    async with session_factory() as session, session.begin():
        await session.execute(
            update(EditorialThreadRow)
            .where(EditorialThreadRow.id == thread.id)
            .values(generation_state="generating", next_sequence=2, version=2)
        )
        session.add(send_event)
        await session.flush()
        session.add(
            EditorialThreadJobRow(
                id=job_id,
                thread_id=thread.id,
                send_event_id=send_event.id,
                state=EditorialThreadJobState.RUNNING.value,
                lease_until=datetime.now(UTC) + timedelta(minutes=30),
                lease_token=uuid4(),
                attempt_count=1,
            )
        )
    return thread.id, job_id


async def _lock_job(uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
    assert await uow.editorial_threads.get_job_for_update(job_id) is not None


async def _persist_terminal_proof(
    session_factory,
    *,
    thread_id: UUID,
    job_id: UUID,
    now: datetime,
) -> UUID:
    conversation_id = uuid4()
    run_id = uuid4()
    turn_id = uuid4()
    input_sha256 = "a" * 64
    output_sha256 = "b" * 64
    async with session_factory() as session, session.begin():
        thread = await session.get(EditorialThreadRow, thread_id)
        job = await session.get(EditorialThreadJobRow, job_id)
        assert thread is not None
        assert job is not None
        idempotency_key = f"editorial-thread-message:{job.send_event_id}"
        session.add(
            ModelConversationRow(
                id=conversation_id,
                provider="openai",
                transport="chatgpt_bridge",
                purpose="editorial_discussion",
                edition_id=None,
                subject_id=thread.subject_id,
                title="Synthetic Bridge generation test",
                status="ready",
                external_id=None,
                external_locator=None,
                expected_profile=None,
                requested_model="test-model",
                head_turn_id=None,
                turn_count=1,
                created_at=now,
                updated_at=now,
                last_used_at=now,
                version=1,
            )
        )
        session.add(
            ModelRunRow(
                id=run_id,
                provider="openai",
                backend="chatgpt_bridge",
                transport="openai_responses",
                model_role="research",
                requested_model="test-model",
                actual_model_version=None,
                prompt_template_id="editorial-test",
                prompt_template_version="1",
                authorized_input_hash=input_sha256,
                evidence_pack_hash="c" * 64,
                parameters={},
                duration_ms=1,
                usage=None,
                status="succeeded",
                submission_state="result_obtained",
                submission_attempt=1,
                bridge_request_id=None,
                response_id=None,
                output_references=["sha256:" + output_sha256],
                error_code=None,
                error_message=None,
                error_details=None,
                raw_output_reference=None,
                raw_output_sha256=None,
                raw_output_chars=None,
                normalized_output_reference=None,
                normalized_output_sha256=None,
                parser_stage=None,
                serializer_version=None,
                normalization_version=None,
                json_error_line=None,
                json_error_column=None,
                validation_errors=[],
                transformations=[],
                citation_count=0,
                extracted_url_count=0,
                visible_citations=[],
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            ModelConversationTurnRow(
                id=turn_id,
                conversation_id=conversation_id,
                sequence=1,
                parent_turn_id=None,
                model_run_id=run_id,
                input_blob_reference="sha256:" + input_sha256,
                input_sha256=input_sha256,
                output_blob_reference="sha256:" + output_sha256,
                output_sha256=output_sha256,
                status="succeeded",
                external_turn_id=None,
                idempotency_key=idempotency_key,
                correlation_id=str(turn_id),
                error_code=None,
                error_message=None,
                error_details=None,
                objective="editorial_message",
                created_at=now,
                started_at=now,
                finished_at=now,
            )
        )
        await session.flush()
        await session.execute(
            update(ModelConversationRow)
            .where(ModelConversationRow.id == conversation_id)
            .values(head_turn_id=turn_id)
        )
        await session.execute(
            update(EditorialThreadRow)
            .where(EditorialThreadRow.id == thread_id)
            .values(model_conversation_id=conversation_id)
        )
    return turn_id


async def _release_test_slot(session_factory) -> None:
    # Repository-only race tests never submit to Bridge; leave the shared test DB free.
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        slot = await uow.bridge_generation_slots.get()
        if slot.state is BridgeGenerationSlotState.FREE:
            return
        assert slot.holder_thread_id is not None
        assert slot.holder_job_id is not None
        released = await uow.bridge_generation_slots.release_after_terminal(
            lease_token=slot.lease_token,
            thread_id=slot.holder_thread_id,
            job_id=slot.holder_job_id,
            turn_id=slot.holder_turn_id,
        )
        assert released
        await uow.commit()


@pytest.mark.asyncio
async def test_postgres_two_connections_race_acquire_vs_acquire(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        first_thread, first_job = await _create_job(session_factory)
        second_thread, second_job = await _create_job(session_factory)
        barrier = asyncio.Barrier(2)
        now = datetime.now(UTC)
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            initial_slot = await uow.bridge_generation_slots.get()
        assert initial_slot.state is BridgeGenerationSlotState.FREE
        initial_token = initial_slot.lease_token

        async def acquire(thread_id: UUID, job_id: UUID):
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await _lock_job(uow, job_id)
                slot = await uow.bridge_generation_slots.get()
                assert slot.state is BridgeGenerationSlotState.FREE
                assert slot.lease_token == initial_token
                await barrier.wait()
                result = await uow.bridge_generation_slots.try_acquire(
                    expected_token=slot.lease_token,
                    previous_outcome=BridgeGenerationPreviousOutcome.UNKNOWN,
                    holder_thread_id=thread_id,
                    holder_job_id=job_id,
                    lease_duration=timedelta(minutes=30),
                    allow_reentry=False,
                    test_now=now,
                )
                await uow.commit()
                return result

        outcomes = await asyncio.gather(
            acquire(first_thread, first_job),
            acquire(second_thread, second_job),
        )
        assert outcomes.count(BridgeGenerationSlotAcquireResult.ACQUIRED) == 1
        assert outcomes.count(BridgeGenerationSlotAcquireResult.STALE) == 1
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            slot = await uow.bridge_generation_slots.get()
        assert slot.state is BridgeGenerationSlotState.HELD
        assert slot.lease_token == initial_token + 1
        assert slot.holder_job_id in {first_job, second_job}
    finally:
        await _release_test_slot(session_factory)
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_reconciliation_release_is_fenced_by_captured_token(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        thread_id, job_id = await _create_job(session_factory)
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            initial = await uow.bridge_generation_slots.get()
            initial_token = initial.lease_token
        async with session_factory() as session, session.begin():
            await session.execute(
                update(BridgeGenerationSlotRow)
                .where(BridgeGenerationSlotRow.singleton_key == 1)
                .values(
                    state=BridgeGenerationSlotState.HELD.value,
                    holder_thread_id=thread_id,
                    holder_job_id=job_id,
                    holder_turn_id=None,
                    acquired_at=datetime.now(UTC),
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    lease_token=initial_token + 1,
                )
            )

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stale_release = await uow.bridge_generation_slots.release_after_reconciliation(
                lease_token=initial_token,
                thread_id=thread_id,
                job_id=job_id,
                turn_id=uuid4(),
                outcome=EditorialThreadReconciliationOutcome.NOT_SUBMITTED,
            )
            assert not stale_release
            current = await uow.bridge_generation_slots.get()
        assert current.state is BridgeGenerationSlotState.HELD
        assert current.lease_token == initial_token + 1
        assert current.holder_job_id == job_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_stale_worker_lease_cannot_acquire_free_slot(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        thread_id, job_id = await _create_job(session_factory)
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            current_job = await uow.editorial_threads.get_job(job_id)
            initial = await uow.bridge_generation_slots.get()
        assert current_job is not None
        assert current_job.lease_token is not None
        assert initial.state is BridgeGenerationSlotState.FREE
        async with session_factory() as session, session.begin():
            await session.execute(
                update(EditorialThreadJobRow)
                .where(EditorialThreadJobRow.id == job_id)
                .values(lease_token=uuid4())
            )

        gate = EditorialBridgeGate(lambda: SqlAlchemyUnitOfWork(session_factory))
        attempt = await gate.acquire(
            thread_id=thread_id,
            job_id=job_id,
            worker_lease_token=current_job.lease_token,
            turn_idempotency_key=f"editorial-key-stale-worker:{uuid4()}",
        )
        assert attempt.disposition is EditorialBridgeGateDisposition.BUSY
        assert attempt.lease is None
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            slot = await uow.bridge_generation_slots.get()
        assert slot.state is BridgeGenerationSlotState.FREE
        assert slot.lease_token == initial.lease_token
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_release_races_expired_takeover_without_clearing_new_fence(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        first_thread, first_job = await _create_job(session_factory)
        second_thread, second_job = await _create_job(session_factory)
        now = datetime.now(UTC)
        terminal_turn_id = await _persist_terminal_proof(
            session_factory,
            thread_id=first_thread,
            job_id=first_job,
            now=now,
        )
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await _lock_job(uow, first_job)
            initial = await uow.bridge_generation_slots.get()
            assert initial.state is BridgeGenerationSlotState.FREE
            initial_token = initial.lease_token
            acquired = await uow.bridge_generation_slots.try_acquire(
                expected_token=initial_token,
                previous_outcome=BridgeGenerationPreviousOutcome.UNKNOWN,
                holder_thread_id=first_thread,
                holder_job_id=first_job,
                lease_duration=timedelta(minutes=30),
                allow_reentry=False,
                test_now=now - timedelta(hours=1),
            )
            assert acquired is BridgeGenerationSlotAcquireResult.ACQUIRED
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            expired_slot = await uow.bridge_generation_slots.get()
        gate = EditorialBridgeGate(lambda: SqlAlchemyUnitOfWork(session_factory))
        previous_outcome = await gate._previous_outcome(expired_slot)
        assert previous_outcome is BridgeGenerationPreviousOutcome.TERMINAL

        barrier = asyncio.Barrier(2)

        async def release_old():
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await _lock_job(uow, first_job)
                await barrier.wait()
                result = await uow.bridge_generation_slots.release_after_terminal(
                    lease_token=initial_token + 1,
                    thread_id=first_thread,
                    job_id=first_job,
                    turn_id=terminal_turn_id,
                )
                await uow.commit()
                return result

        async def take_over():
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await _lock_job(uow, second_job)
                await barrier.wait()
                result = await uow.bridge_generation_slots.try_acquire(
                    expected_token=initial_token + 1,
                    previous_outcome=previous_outcome,
                    holder_thread_id=second_thread,
                    holder_job_id=second_job,
                    lease_duration=timedelta(minutes=30),
                    allow_reentry=False,
                    test_now=now,
                )
                await uow.commit()
                return result

        released, takeover = await asyncio.gather(release_old(), take_over())
        assert takeover is BridgeGenerationSlotAcquireResult.ACQUIRED
        assert isinstance(released, bool)
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stale_release = await uow.bridge_generation_slots.release_after_terminal(
                lease_token=initial_token + 1,
                thread_id=first_thread,
                job_id=first_job,
                turn_id=terminal_turn_id,
            )
            assert not stale_release
            slot = await uow.bridge_generation_slots.get()
        assert slot.state is BridgeGenerationSlotState.HELD
        assert slot.holder_thread_id == second_thread
        assert slot.holder_job_id == second_job
        assert slot.lease_token == initial_token + 2
    finally:
        await _release_test_slot(session_factory)
        await engine.dispose()


@pytest.mark.asyncio
async def test_bridge_generation_slot_rejects_delete_and_truncate(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            with pytest.raises(DBAPIError):
                await session.execute(delete(BridgeGenerationSlotRow))
        async with session_factory() as session, session.begin():
            with pytest.raises(DBAPIError):
                await session.execute(text("TRUNCATE TABLE bridge_generation_slots"))
        async with session_factory() as session, session.begin():
            slot = await session.get(BridgeGenerationSlotRow, 1)
            assert slot is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_expired_takeover_without_proof_retains_unknown_fence(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        first_thread, first_job = await _create_job(session_factory)
        second_thread, second_job = await _create_job(session_factory)
        now = datetime.now(UTC)
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await _lock_job(uow, first_job)
            initial = await uow.bridge_generation_slots.get()
            assert initial.state is BridgeGenerationSlotState.FREE
            initial_token = initial.lease_token
            acquired = await uow.bridge_generation_slots.try_acquire(
                expected_token=initial_token,
                previous_outcome=BridgeGenerationPreviousOutcome.UNKNOWN,
                holder_thread_id=first_thread,
                holder_job_id=first_job,
                lease_duration=timedelta(minutes=30),
                allow_reentry=False,
                test_now=now - timedelta(hours=1),
            )
            assert acquired is BridgeGenerationSlotAcquireResult.ACQUIRED
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            acquired_slot = await uow.bridge_generation_slots.get()
        held_token = acquired_slot.lease_token
        assert held_token == initial_token + 1

        gate = EditorialBridgeGate(lambda: SqlAlchemyUnitOfWork(session_factory))
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            second_worker_job = await uow.editorial_threads.get_job(second_job)
        assert second_worker_job is not None
        assert second_worker_job.lease_token is not None
        takeover = await gate.acquire(
            thread_id=second_thread,
            job_id=second_job,
            worker_lease_token=second_worker_job.lease_token,
            turn_idempotency_key=f"editorial-key-second:{uuid4()}",
            test_now=now,
        )
        assert takeover.disposition is EditorialBridgeGateDisposition.BUSY
        assert takeover.lease is None
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            slot = await uow.bridge_generation_slots.get()
        assert slot.state is BridgeGenerationSlotState.HELD_UNKNOWN
        assert slot.holder_thread_id == first_thread
        assert slot.holder_job_id == first_job
        assert slot.lease_token == held_token
        async with session_factory() as session:
            job = await session.get(EditorialThreadJobRow, first_job)
            thread = await session.get(EditorialThreadRow, first_thread)
        assert job is not None
        assert job.state == EditorialThreadJobState.RUNNING.value
        assert thread is not None
        assert thread.generation_state == EditorialBridgeStatus.RECONCILIATION_REQUIRED.value
    finally:
        await engine.dispose()

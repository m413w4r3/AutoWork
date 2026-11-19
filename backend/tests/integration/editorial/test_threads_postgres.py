from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.application.blob_storage import BlobStore
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import ModelGateway
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
    EditorialThreadError,
    EditorialThreadEventKind,
    EditorialThreadJobState,
)
from cti_app.domain.entities import Subject
from cti_app.domain.model_conversations import ConversationPurpose, ConversationTransport
from cti_app.domain.model_runs import ModelProvider
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage, ProductionRun
from cti_app.infrastructure.database.models.editorial_threads import (
    EditorialThreadEventRow,
    EditorialThreadJobRow,
    EditorialThreadRow,
)
from cti_app.infrastructure.database.repositories.editorial_threads import (
    SqlAlchemyEditorialThreadRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


async def _create_thread_context(session_factory):
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Synthetic editorial thread subject",
            slug=f"thread-{uuid4().hex[:12]}",
            tlp=TLP.AMBER,
        )
        await uow.subjects.add(subject)
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        await uow.production_runs.add(run)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="1" * 64,
        )
        await uow.production_artifacts.append(artifact)
        await uow.commit()
    thread = EditorialThreadRow(
        id=uuid4(),
        subject_id=subject.id,
        production_run_id=run.id,
        occurrence_id=uuid4(),
        session_number=1,
        origin_artifact_id=artifact.id,
        origin_artifact_version=1,
        origin_canonical_sha256="2" * 64,
        model_conversation_id=None,
        generation_state="ready",
        next_sequence=1,
        version=1,
    )
    async with session_factory() as session, session.begin():
        session.add(thread)
    return thread


async def _event(thread_id, sequence: int, idempotency_key: str) -> EditorialThreadEventRow:
    return EditorialThreadEventRow(
        id=uuid4(),
        thread_id=thread_id,
        sequence=sequence,
        event_kind="user_message",
        actor_id=uuid4(),
        idempotency_key=idempotency_key,
        request_sha256=(str(sequence) * 64)[:64],
        payload_blob_ref=None,
        payload_sha256=None,
        proposal_id=None,
        model_turn_id=None,
    )


@pytest.mark.asyncio
async def test_editorial_session_retry_recovers_after_conversation_commit(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        thread = await _create_thread_context(session_factory)
        session_thread_id = uuid4()
        conversation_id = uuid5(NAMESPACE_URL, f"editorial-thread-conversation:{session_thread_id}")
        service = ModelConversationService(
            lambda: SqlAlchemyUnitOfWork(session_factory),
            cast(ModelGateway, object()),
            cast(BlobStore, object()),
        )
        arguments = {
            "provider": ModelProvider.OPENAI,
            "transport": ConversationTransport.CHATGPT_BRIDGE,
            "purpose": ConversationPurpose.EDITORIAL_DISCUSSION,
            "title": "Discussion éditoriale",
            "edition_id": None,
            "subject_id": thread.subject_id,
            "expected_profile": None,
            "requested_model": None,
            "_editorial_thread_id": session_thread_id,
            "_allow_missing_editorial_thread_recovery": True,
        }

        # The first commit persists the private conversation. Omitting the
        # subsequent thread insert models a crash between the two commits.
        created = await service.get_or_create(conversation_id, **arguments)
        recovered = await service.get_or_create(conversation_id, **arguments)

        assert recovered.id == created.id == conversation_id
        assert recovered.subject_id == thread.subject_id
        assert recovered.purpose is ConversationPurpose.EDITORIAL_DISCUSSION
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editorial_threads.get(session_thread_id) is None
            persisted = await uow.model_conversations.get(conversation_id)
        assert persisted is not None
        assert persisted.id == recovered.id
    finally:
        await engine.dispose()


async def test_thread_uniqueness_append_only_origin_jobs_and_no_sql_prose(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        thread = await _create_thread_context(session_factory)
        duplicate = EditorialThreadRow(
            id=uuid4(),
            subject_id=thread.subject_id,
            production_run_id=thread.production_run_id,
            occurrence_id=thread.occurrence_id,
            session_number=thread.session_number,
            origin_artifact_id=thread.origin_artifact_id,
            origin_artifact_version=thread.origin_artifact_version,
            origin_canonical_sha256=thread.origin_canonical_sha256,
            model_conversation_id=None,
            generation_state="ready",
            next_sequence=1,
            version=1,
        )
        with pytest.raises(IntegrityError):
            async with session_factory() as session, session.begin():
                session.add(duplicate)
                await session.flush()

        async with session_factory() as session, session.begin():
            await session.execute(
                update(EditorialThreadRow)
                .where(EditorialThreadRow.id == thread.id)
                .values(generation_state="session_lost")
            )

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            lost = await uow.editorial_threads.get(thread.id)
            assert lost is not None
            next_session = lost.thread.start_new_session(
                origin=lost.thread.origin,
                model_conversation_id=None,
            )
            persisted = await uow.editorial_threads.create_if_absent(next_session)
            await uow.commit()
        assert persisted.session_number == 2

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            subject = await uow.subjects.get(thread.subject_id)
            assert subject is not None
            other_run = ProductionRun(
                subject_id=subject.id, edition_id=subject.edition_id, run_number=2
            )
            other_run.mark_cancelled()
            await uow.production_runs.add(other_run)
            other_artifact = ProductionArtifact(
                production_run_id=other_run.id,
                subject_id=subject.id,
                stage=ProductionArtifactStage.SYNTHESIS,
                version=1,
                input_hash="3" * 64,
            )
            await uow.production_artifacts.append(other_artifact)
            await uow.commit()
        other_run_thread = EditorialThreadRow(
            id=uuid4(),
            subject_id=thread.subject_id,
            production_run_id=other_run.id,
            occurrence_id=thread.occurrence_id,
            session_number=3,
            origin_artifact_id=other_artifact.id,
            origin_artifact_version=1,
            origin_canonical_sha256="4" * 64,
            model_conversation_id=None,
            generation_state="ready",
            next_sequence=1,
            version=1,
        )
        async with session_factory() as session, session.begin():
            session.add(other_run_thread)

        async with session_factory() as session:
            current = await SqlAlchemyEditorialThreadRepository(session).get_current_by_occurrence(
                thread.subject_id, thread.production_run_id, thread.occurrence_id
            )
        assert current is not None
        assert current.thread.session_number == 2
        assert current.thread.origin.artifact_id == thread.origin_artifact_id

        duplicate_current = EditorialThreadRow(
            id=uuid4(),
            subject_id=thread.subject_id,
            production_run_id=thread.production_run_id,
            occurrence_id=thread.occurrence_id,
            session_number=2,
            origin_artifact_id=thread.origin_artifact_id,
            origin_artifact_version=thread.origin_artifact_version,
            origin_canonical_sha256=thread.origin_canonical_sha256,
            model_conversation_id=None,
            generation_state="ready",
            next_sequence=1,
            version=1,
        )
        with pytest.raises(IntegrityError):
            async with session_factory() as session, session.begin():
                session.add(duplicate_current)
                await session.flush()

        first = await _event(thread.id, 1, "synthetic-message-one")
        async with session_factory() as session, session.begin():
            session.add(first)
            session.add(await _event(thread.id, 2, "synthetic-message-two"))

        with pytest.raises(IntegrityError):
            async with session_factory() as session, session.begin():
                session.add(await _event(thread.id, 1, "duplicate-sequence"))
                await session.flush()
        with pytest.raises(IntegrityError):
            async with session_factory() as session, session.begin():
                session.add(await _event(thread.id, 3, "synthetic-message-one"))
                await session.flush()

        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(EditorialThreadRow)
                    .where(EditorialThreadRow.id == thread.id)
                    .values(origin_artifact_version=2)
                )
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(EditorialThreadRow)
                    .where(EditorialThreadRow.id == thread.id)
                    .values(session_number=3)
                )
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(EditorialThreadEventRow)
                    .where(EditorialThreadEventRow.id == first.id)
                    .values(request_sha256="f" * 64)
                )
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    delete(EditorialThreadEventRow).where(EditorialThreadEventRow.id == first.id)
                )
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(text("TRUNCATE TABLE editorial_thread_events"))

        async with session_factory() as session, session.begin():
            session.add(
                EditorialThreadJobRow(
                    id=uuid4(),
                    thread_id=thread.id,
                    send_event_id=first.id,
                    state="queued",
                    lease_until=None,
                    lease_token=None,
                    attempt_count=0,
                )
            )
        third = await _event(thread.id, 3, "synthetic-message-three")
        async with session_factory() as session, session.begin():
            session.add(third)
        with pytest.raises(IntegrityError):
            async with session_factory() as session, session.begin():
                session.add(
                    EditorialThreadJobRow(
                        id=uuid4(),
                        thread_id=thread.id,
                        send_event_id=third.id,
                        state="running",
                        lease_until=None,
                        lease_token=None,
                        attempt_count=0,
                    )
                )
                await session.flush()

        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: inspect(sync).get_columns("editorial_thread_events")
            )
        column_names = {column["name"] for column in columns}
        assert not {"body", "body_text", "message", "prose"} & column_names
    finally:
        await engine.dispose()


async def test_thread_event_transaction_rolls_back_as_one_unit(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        thread = await _create_thread_context(session_factory)
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await uow.editorial_threads.append_event(
                    thread_id=thread.id,
                    kind=EditorialThreadEventKind.USER_MESSAGE,
                    actor_id=None,
                    idempotency_key="rollback-only",
                    request_sha256="a" * 64,
                )
                raise RuntimeError("synthetic rollback")
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(EditorialThreadEventRow)
                .where(EditorialThreadEventRow.thread_id == thread.id)
            )
            sequence = await session.scalar(
                select(EditorialThreadRow.next_sequence).where(EditorialThreadRow.id == thread.id)
            )
        assert count == 0
        assert sequence == 1
    finally:
        await engine.dispose()


async def test_post_races_claim_and_completion_with_thread_then_job_lock_order(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        thread = await _create_thread_context(session_factory)
        send_event = await _event(thread.id, 1, "race-initial-message")
        job_id = uuid4()
        async with session_factory() as session, session.begin():
            session.add(send_event)
            session.add(
                EditorialThreadJobRow(
                    id=job_id,
                    thread_id=thread.id,
                    send_event_id=send_event.id,
                    state=EditorialThreadJobState.QUEUED.value,
                    lease_until=None,
                    lease_token=None,
                    attempt_count=0,
                )
            )
            row = await session.get(EditorialThreadRow, thread.id)
            assert row is not None
            row.next_sequence = 2
            row.version = 2
            row.generation_state = EditorialBridgeStatus.GENERATING.value

        async def post(key: str) -> str:
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                try:
                    await uow.editorial_threads.enqueue_message(
                        job_id=uuid4(),
                        thread_id=thread.id,
                        actor_id=uuid4(),
                        idempotency_key=key,
                        request_sha256=hashlib.sha256(key.encode()).hexdigest(),
                        payload_blob_ref=None,  # type: ignore[arg-type]
                        payload_sha256=None,  # type: ignore[arg-type]
                    )
                except EditorialThreadError as error:
                    await uow.rollback()
                    return error.code if error.code != "editorial_thread_error" else str(error)
                await uow.commit()
                return "accepted"

        now = datetime.now(UTC)
        claim_token = uuid4()
        lease_until = now + timedelta(minutes=1)

        async def claim():
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                claimed = await uow.editorial_threads.claim_job(
                    job_id,
                    lease_token=claim_token,
                    lease_until=lease_until,
                    now=now,
                )
                await uow.commit()
                return claimed

        claim_barrier = asyncio.Barrier(2)

        async def simultaneous_claim_post():
            async def run_post() -> str:
                await claim_barrier.wait()
                return await post("post-vs-claim")

            async def run_claim():
                await claim_barrier.wait()
                return await claim()

            return await asyncio.wait_for(asyncio.gather(run_post(), run_claim()), timeout=10)

        post_claim_result, claimed = await simultaneous_claim_post()
        assert post_claim_result == "editorial_thread_busy"
        assert claimed is not None

        async def complete() -> None:
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await uow.editorial_threads.append_event(
                    thread_id=thread.id,
                    kind=EditorialThreadEventKind.BRIDGE_REPLY,
                    actor_id=None,
                    idempotency_key="race-completion-reply",
                    request_sha256="c" * 64,
                    payload_blob_ref=None,
                    payload_sha256=None,
                )
                await uow.editorial_threads.set_job_state(
                    job_id,
                    EditorialThreadJobState.COMPLETE,
                    lease_token=claim_token,
                )
                await uow.editorial_threads.set_bridge_status(
                    thread.id, EditorialBridgeStatus.REPLY_AVAILABLE
                )
                await uow.commit()

        completion_barrier = asyncio.Barrier(2)

        async def simultaneous_completion_post():
            async def run_post() -> str:
                await completion_barrier.wait()
                return await post("post-vs-completion")

            async def run_complete() -> None:
                await completion_barrier.wait()
                await complete()

            return await asyncio.wait_for(asyncio.gather(run_post(), run_complete()), timeout=10)

        post_completion_result, _ = await simultaneous_completion_post()
        assert post_completion_result in {"editorial_thread_busy", "accepted"}
    finally:
        await engine.dispose()

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from cti_app.domain.entities import Subject
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

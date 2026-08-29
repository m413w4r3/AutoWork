from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.editions import EditionService
from cti_app.application.jobs import JobService, create_job_registry
from cti_app.application.persistence import EditionUnitOfWork, JobUnitOfWork, UnitOfWork
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.entities import ProvenanceEvent, Sample, SourceDocument, Subject
from cti_app.domain.errors import BlobStillReferencedError
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import (
    ModelOutputRejection,
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.core import ProvenanceEventRow, SubjectRow
from cti_app.infrastructure.database.models.editions import EditionAuditEventRow, EditionRow
from cti_app.infrastructure.database.models.jobs import JobEventRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_unit_of_work_commits_and_rolls_back(migrated_postgres_url: str) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    committed = Subject(external_id="SUBJ-COMMITTED", slug="committed", tlp=TLP.AMBER)
    rolled_back = Subject(external_id="SUBJ-ROLLED-BACK", slug="rolled-back", tlp=TLP.GREEN)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.subjects.add(committed)
            await uow.commit()

        with pytest.raises(RuntimeError, match="force rollback"):
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await uow.subjects.add(rolled_back)
                raise RuntimeError("force rollback")

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.subjects.get(committed.id) == committed
            assert await uow.subjects.get(rolled_back.id) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_prevents_tlp_downgrade(migrated_postgres_url: str) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    subject = Subject(external_id="SUBJ-TLP-GUARD", slug="tlp-guard", tlp=TLP.RED)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.subjects.add(subject)
            await uow.commit()

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(SubjectRow).where(SubjectRow.id == subject.id).values(tlp="GREEN")
                )

        async with engine.connect() as connection:
            persisted_tlp = await connection.scalar(
                select(SubjectRow.tlp).where(SubjectRow.id == subject.id)
            )
        assert persisted_tlp == "RED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provenance_events_are_append_only(migrated_postgres_url: str) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    subject = Subject(external_id="SUBJ-PROVENANCE", slug="provenance", tlp=TLP.AMBER)
    event = ProvenanceEvent(
        subject_id=subject.id,
        aggregate_type="subject",
        aggregate_id=subject.id,
        event_type="subject.created",
        payload={"source": "test"},
        tlp=TLP.AMBER,
        actor_id="test-suite",
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.subjects.add(subject)
            await uow.provenance.append(event)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert list(await uow.provenance.list_for_aggregate("subject", subject.id)) == [event]

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(ProvenanceEventRow)
                    .where(ProvenanceEventRow.id == event.id)
                    .values(event_type="tampered")
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    delete(ProvenanceEventRow).where(ProvenanceEventRow.id == event.id)
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_edition_audit_and_job_transitions_are_append_only(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def edition_uow_factory() -> EditionUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    def job_uow_factory() -> JobUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    edition_service = EditionService(edition_uow_factory)
    job_service = JobService(job_uow_factory, create_job_registry())
    try:
        edition = await edition_service.create(
            country="Iran",
            country_code="IR",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr", "fa"),
            target_articles=8,
            previous_edition_id=None,
            source_profile="iran-default",
            actor_id="dev-analyst",
            correlation_id="edition-integration",
        )
        job = await job_service.submit(
            kind="demo.deterministic",
            aggregate_type="edition",
            aggregate_id=edition.id,
            idempotency_key=f"edition-demo-{uuid4()}",
            correlation_id="job-integration",
            input_parameters={"steps": 1},
            actor_id="dev-analyst",
        )

        assert (await edition_service.audit(edition.id))[0].actor_id == "dev-analyst"
        assert (await job_service.history(job.id))[0].actor_id == "dev-analyst"

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(EditionRow).where(EditionRow.id == edition.id).values(tlp="GREEN")
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(EditionAuditEventRow)
                    .where(EditionAuditEventRow.edition_id == edition.id)
                    .values(action="tampered")
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(delete(JobEventRow).where(JobEventRow.job_id == job.id))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_model_run_round_trip_never_persists_prompt_content(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    run = ModelRun(
        provider=ModelProvider.OPENAI,
        model_role=ModelRole.RESEARCH,
        requested_model="chatgpt-web",
        prompt_template_id="research-monthly",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={"reasoning": {"effort": "high"}},
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.model_runs.add(run)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.model_runs.get_for_update(run.id)
            assert persisted is not None
            persisted.wait_for_background(
                response_id="resp_integration",
                actual_model_version="chatgpt-web",
                usage=None,
            )
            persisted.raw_output_reference = "blob://11111111-1111-4111-8111-111111111111"
            persisted.raw_output_sha256 = "c" * 64
            persisted.raw_output_chars = 42
            persisted.parser_stage = "pydantic_validation"
            persisted.normalization_version = "discovery-json-v1"
            persisted.validation_errors = (
                {"path": ["topics", "0", "sources", "1", "url"], "code": "value_error"},
            )
            await uow.model_runs.save(persisted)
            await uow.model_output_rejections.append(
                ModelOutputRejection(
                    model_run_id=run.id,
                    path=("topics", "0", "sources", "1", "url"),
                    error_type="value_error",
                    value_sha256="d" * 64,
                    raw_output_reference=persisted.raw_output_reference,
                )
            )
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.model_runs.get(run.id)
        assert persisted is not None
        assert persisted.status is ModelRunStatus.WAITING_BACKGROUND
        assert persisted.response_id == "resp_integration"
        assert persisted.raw_output_sha256 == "c" * 64
        assert persisted.validation_errors[0]["code"] == "value_error"
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            rejections = await uow.model_output_rejections.list_for_run(run.id)
        assert rejections[0].path[-1] == "url"
        assert not hasattr(persisted, "prompt")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_model_conversation_and_turn_round_trip(migrated_postgres_url: str) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    subject = Subject(
        external_id=f"SUBJ-CONVERSATION-{uuid4().hex}",
        slug=f"conversation-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    conversation = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.ANALYST_ASSISTANCE,
        subject_id=subject.id,
        title="Analyse persistante",
    )
    run = ModelRun(
        provider=ModelProvider.OPENAI,
        model_role=ModelRole.RESEARCH,
        requested_model="chatgpt-web",
        prompt_template_id="analyst-conversation",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.subjects.add(subject)
            await uow.model_conversations.add(conversation)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.model_conversations.get_for_update(conversation.id)
            assert persisted is not None
            persisted.start_turn(mode=ConversationMode.FRESH)
            turn = ModelConversationTurn(
                conversation_id=persisted.id,
                sequence=persisted.turn_count,
                model_run_id=run.id,
                input_blob_reference=f"blob://{uuid4()}",
                input_sha256="c" * 64,
                idempotency_key=f"turn-{uuid4()}",
                correlation_id="integration-conversation",
            )
            await uow.model_runs.add(run)
            await uow.model_conversation_turns.add(turn)
            await uow.model_conversations.save(persisted)
            await uow.commit()

        competing_run = ModelRun(
            provider=ModelProvider.OPENAI,
            model_role=ModelRole.RESEARCH,
            requested_model="chatgpt-web",
            prompt_template_id="analyst-conversation",
            prompt_template_version="1",
            authorized_input_hash="e" * 64,
            evidence_pack_hash="b" * 64,
            parameters={},
        )
        competing_turn = ModelConversationTurn(
            conversation_id=conversation.id,
            sequence=2,
            model_run_id=competing_run.id,
            input_blob_reference=f"blob://{uuid4()}",
            input_sha256="f" * 64,
            idempotency_key=f"turn-{uuid4()}",
            correlation_id="integration-competing-turn",
        )
        with pytest.raises(DBAPIError):
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await uow.model_runs.add(competing_run)
                await uow.model_conversation_turns.add(competing_turn)
                await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.model_conversations.get_for_update(conversation.id)
            persisted_turn = await uow.model_conversation_turns.get(turn.id)
            assert persisted is not None and persisted_turn is not None
            persisted_turn.succeed(
                output_blob_reference=f"blob://{uuid4()}",
                output_sha256="d" * 64,
                external_turn_id="opaque-turn",
            )
            persisted.finish_turn(
                persisted_turn.id,
                external_locator="https://chatgpt.com/opaque/conversation",
            )
            await uow.model_conversation_turns.save(persisted_turn)
            await uow.model_conversations.save(persisted)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored = await uow.model_conversations.get(conversation.id)
            stored_turns = await uow.model_conversation_turns.list_for_conversation(conversation.id)
        assert stored is not None
        assert stored.status is ConversationStatus.READY
        assert stored.turn_count == 1
        assert stored.head_turn_id == turn.id
        assert stored_turns[0].status is ConversationTurnStatus.SUCCEEDED
        assert stored_turns[0].correlation_id == "integration-conversation"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_document_and_sample_keep_distinct_semantics(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    subject = Subject(
        external_id=f"SUBJ-SEMANTICS-{uuid4().hex}",
        slug=f"semantics-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256="a" * 64,
            size=1,
            mime_type="application/octet-stream",
            logical_bucket=f"shared-{uuid4().hex[:8]}",
        )
    )
    acquired_at = datetime(2026, 8, 7, tzinfo=UTC)
    document = SourceDocument(
        subject_id=subject.id,
        blob_id=blob.id,
        original_name="analysis.pdf",
        origin="publisher",
        acquired_at=acquired_at,
        license_restriction="citation only",
        tlp=TLP.GREEN,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    sample = Sample(
        subject_id=subject.id,
        blob_id=blob.id,
        original_name="payload.bin",
        origin="report-attachment",
        acquired_at=acquired_at,
        license_restriction="internal only",
        tlp=TLP.RED,
        do_not_submit=True,
        external_llm_allowed=False,
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.blobs.add(blob)
            await uow.subjects.add(subject)
            await uow.source_documents.add(document)
            await uow.samples.add(sample)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert list(await uow.source_documents.list_for_subject(subject.id)) == [document]
            assert list(await uow.samples.list_for_subject(subject.id)) == [sample]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_referenced_blob_cannot_be_physically_deleted(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    store = FilesystemBlobStore(tmp_path / "blobs")

    def uow_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    service = BlobCatalogService(store, uow_factory)
    subject = Subject(
        external_id=f"SUBJ-BLOB-{uuid4().hex}", slug=f"blob-{uuid4().hex}", tlp=TLP.RED
    )
    acquired_at = datetime(2026, 8, 7, tzinfo=UTC)
    try:
        blob = await service.ingest(
            BytesIO(b"referenced content"),
            logical_bucket="samples",
            mime_type="application/octet-stream",
        )
        document = SourceDocument(
            subject_id=subject.id,
            blob_id=blob.id,
            original_name="sample.bin",
            origin="test-fixture",
            acquired_at=acquired_at,
            license_restriction=None,
            tlp=TLP.RED,
            do_not_submit=True,
            external_llm_allowed=False,
        )
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.subjects.add(subject)
            await uow.source_documents.add(document)
            await uow.commit()

        with pytest.raises(BlobStillReferencedError):
            await service.delete_unreferenced(blob.id)
        assert await store.exists(blob.descriptor)
    finally:
        await engine.dispose()

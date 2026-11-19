from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalModelRun,
    EditorialProposalOrigin,
    EditorialProposalState,
    variant_group_identity,
)
from cti_app.domain.entities import Subject
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage, ProductionRun
from cti_app.infrastructure.database.models.editorial_proposals import (
    EditorialProposalEventRow,
    EditorialProposalModelRunRow,
    EditorialProposalRow,
)
from cti_app.infrastructure.database.models.editorial_threads import EditorialThreadRow
from cti_app.infrastructure.database.models.editorial_workspace import (
    EditorialWorkspaceSnapshotRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationRow,
    ModelConversationTurnRow,
    ModelRunRow,
)
from cti_app.infrastructure.database.models.production import ProductionArtifactRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


async def test_proposal_persistence_is_exact_idempotent_and_append_only(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    now = datetime.now(UTC)
    try:
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
                title="Synthetic proposal persistence subject",
                slug=f"proposal-{uuid4().hex[:12]}",
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

        snapshot_id = uuid4()
        occurrence_id = uuid4()
        thread_id = uuid4()
        conversation_id = uuid4()
        model_run_id = uuid4()
        model_turn_id = uuid4()
        patch_bytes = b'{"schema_version":"editorial-patch-v1","synthetic":true}'
        patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
        blob = BlobRecord(
            descriptor=BlobDescriptor(
                sha256=patch_sha256,
                size=len(patch_bytes),
                mime_type="application/vnd.autowork.editorial-patch+json",
                logical_bucket="editorial-proposal-patches",
            )
        )

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored_blob = await uow.blobs.add_if_absent(blob)
            assert stored_blob.id == blob.id
            await uow.commit()

        async with session_factory() as session, session.begin():
            session.add(
                ModelRunRow(
                    id=model_run_id,
                    provider="fake",
                    backend="fake",
                    transport="fake",
                    model_role="drafting",
                    requested_model="synthetic",
                    prompt_template_id="synthetic-editorial-proposal",
                    prompt_template_version="1",
                    authorized_input_hash="2" * 64,
                    evidence_pack_hash="3" * 64,
                    parameters={},
                    status="succeeded",
                    submission_state="result_obtained",
                    submission_attempt=1,
                    output_references=["synthetic-model-output"],
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

            conversation = ModelConversationRow(
                id=conversation_id,
                provider="fake",
                transport="application_managed",
                purpose="editorial_discussion",
                edition_id=edition.id,
                subject_id=subject.id,
                title="Synthetic editorial conversation",
                status="ready",
                head_turn_id=None,
                turn_count=0,
                created_at=now,
                updated_at=now,
                last_used_at=None,
                version=1,
            )
            session.add(conversation)
            await session.flush()

            session.add(
                ModelConversationTurnRow(
                    id=model_turn_id,
                    conversation_id=conversation_id,
                    sequence=1,
                    parent_turn_id=None,
                    model_run_id=model_run_id,
                    input_blob_reference="synthetic-input",
                    input_sha256="4" * 64,
                    output_blob_reference="synthetic-output",
                    output_sha256="5" * 64,
                    status="succeeded",
                    external_turn_id=None,
                    idempotency_key=f"synthetic-turn-{uuid4().hex}",
                    correlation_id="synthetic-correlation",
                    error_code=None,
                    error_message=None,
                    error_details=None,
                    objective="editorial_proposal",
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                )
            )
            await session.flush()
            conversation.head_turn_id = model_turn_id

            session.add(
                EditorialWorkspaceSnapshotRow(
                    snapshot_id=snapshot_id,
                    subject_id=subject.id,
                    production_run_id=run.id,
                    synthesis_artifact_id=artifact.id,
                    synthesis_version=1,
                    synthesis_input_hash="1" * 64,
                    synthesis_canonical_sha256="6" * 64,
                    enrichment_artifact_id=None,
                    enrichment_version=None,
                    enrichment_input_hash=None,
                    enrichment_canonical_sha256=None,
                    publication_artifact_id=None,
                    publication_version=None,
                    publication_input_hash=None,
                    publication_canonical_sha256=None,
                    pipeline_generation=0,
                    routing_decision_id=None,
                    routing_decision_hash=None,
                    publication_kind="article",
                    workspace_pin_hash="7" * 64,
                    registry_hash="8" * 64,
                    block_registry=[],
                    retired_block_ids=[],
                    parent_snapshot_id=None,
                )
            )
            session.add(
                EditorialThreadRow(
                    id=thread_id,
                    subject_id=subject.id,
                    production_run_id=run.id,
                    occurrence_id=occurrence_id,
                    session_number=1,
                    origin_artifact_id=artifact.id,
                    origin_artifact_version=1,
                    origin_canonical_sha256="6" * 64,
                    model_conversation_id=conversation_id,
                    generation_state="reply_available",
                    next_sequence=2,
                    version=2,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

        actor_id = uuid4()
        idempotency_key = f"synthetic-proposal-{uuid4().hex}"
        proposal = EditorialProposal(
            id=uuid4(),
            subject_id=subject.id,
            production_run_id=run.id,
            origin=EditorialProposalOrigin.BRIDGE_DRAFT,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            base_artifact_id=artifact.id,
            base_artifact_version=1,
            base_input_hash="1" * 64,
            base_canonical_sha256="6" * 64,
            base_routing_hash=None,
            base_pin_hash="7" * 64,
            target_id="synthetic-target",
            operation="rewrite_title",
            before_hash="9" * 64,
            patch_blob_ref=blob.id,
            patch_sha256=patch_sha256,
            evidence_refs=(),
            diagnostics={},
            thread_id=thread_id,
            variant_group_id=variant_group_identity(
                base_pin_hash="7" * 64,
                occurrence_id=occurrence_id,
                operation="rewrite_title",
                thread_id=thread_id,
            ),
            model_run_id=model_run_id,
            model_turn_id=model_turn_id,
            state=EditorialProposalState.PROPOSED,
            state_version=1,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_sha256="a" * 64,
        )
        event = EditorialProposalEvent(
            id=uuid4(),
            proposal_id=proposal.id,
            sequence=1,
            kind=EditorialProposalEventKind.PREPARED,
            state=EditorialProposalState.PROPOSED,
            state_version=1,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_sha256=proposal.request_sha256,
            patch_sha256=patch_sha256,
        )
        model_link = EditorialProposalModelRun(
            proposal_id=proposal.id,
            thread_id=thread_id,
            model_run_id=model_run_id,
            model_turn_id=model_turn_id,
        )

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored, inserted = await uow.editorial_proposals.add_if_absent(
                proposal, event=event, model_run=model_link
            )
            assert inserted
            assert stored.model_run_id == model_run_id
            assert stored.model_turn_id == model_turn_id
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.editorial_proposals.get_for_occurrence(
                subject.id, occurrence_id, proposal.id
            )
            assert persisted is not None
            assert persisted.state is EditorialProposalState.PROPOSED
            assert persisted.patch_sha256 == patch_sha256
            assert persisted.model_run_id == model_run_id
            assert persisted.model_turn_id == model_turn_id

            replay, replayed = await uow.editorial_proposals.add_if_absent(
                proposal, event=event, model_run=model_link
            )
            assert not replayed
            assert replay.id == proposal.id

        async with session_factory() as session:
            link = await session.get(EditorialProposalModelRunRow, proposal.id)
            event_count = await session.scalar(
                select(func.count())
                .select_from(EditorialProposalEventRow)
                .where(EditorialProposalEventRow.proposal_id == proposal.id)
            )
            artifact_count = await session.scalar(
                select(func.count())
                .select_from(ProductionArtifactRow)
                .where(ProductionArtifactRow.production_run_id == run.id)
            )
        assert link is not None
        assert link.model_run_id == model_run_id
        assert link.model_turn_id == model_turn_id
        assert event_count == 1
        assert artifact_count == 1

        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(EditorialProposalRow)
                    .where(EditorialProposalRow.id == proposal.id)
                    .values(state="accepted", state_version=2)
                )

        async with session_factory() as session:
            persisted_row = await session.get(EditorialProposalRow, proposal.id)
            assert persisted_row is not None
            accepted_values = {
                column.name: getattr(persisted_row, column.name)
                for column in EditorialProposalRow.__table__.columns
            }
        accepted_values.update(
            id=uuid4(),
            state="accepted",
            state_version=1,
            idempotency_key=f"accepted-{uuid4().hex}",
            decision_id=None,
            accepted_artifact_id=None,
        )
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(insert(EditorialProposalRow).values(**accepted_values))

        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(EditorialProposalRow)
                    .where(EditorialProposalRow.id == proposal.id)
                    .values(operation="rewrite_lead")
                )
        with pytest.raises(DBAPIError):
            async with session_factory() as session, session.begin():
                await session.execute(text("TRUNCATE TABLE editorial_proposal_events"))
    finally:
        await engine.dispose()

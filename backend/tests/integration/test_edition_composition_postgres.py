from __future__ import annotations

import asyncio
import calendar
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.application.edition_composition import (
    CompositionIdempotencyConflict,
    CompositionReferenceError,
    CompositionValidationCode,
    CompositionVersionConflict,
    EditionCompositionService,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.edition_composition import (
    CompositionDisposition,
    EditionCompositionEntry,
)
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionBatchStatus,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.publication_review import PublicationDecision, PublicationReviewDecision
from cti_app.infrastructure.database.models.edition_composition import (
    EditionCompositionEntryRow,
    EditionCompositionEventRow,
)
from cti_app.infrastructure.database.models.production import ProductionArtifactRow

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _PinnedWorld:
    edition: Edition
    subject: Subject
    run: ProductionRun
    artifact: ProductionArtifact
    batch: EditionProductionBatch


def _new_edition(country_code: str = "FR") -> Edition:
    token = uuid4().int
    year = 2000 + (token >> 8) % 7000
    month = token % 12 + 1
    return Edition(
        country=f"Composition {country_code}",
        country_code=country_code,
        period_start=date(year, month, 1),
        period_end=date(year, month, calendar.monthrange(year, month)[1]),
        tlp=TLP.GREEN,
        languages=("fr",),
    )


async def _persist_pinned_world(
    uow_factory: UnitOfWorkFactory, *, country_code: str = "FR"
) -> _PinnedWorld:
    edition = _new_edition(country_code)
    subject = Subject(
        edition_id=edition.id,
        title="Composition integration subject",
        slug=f"composition-{uuid4().hex}",
        tlp=TLP.GREEN,
        version=1,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        pipeline_generation=2,
    )
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status=ProductionBatchStatus.COMPLETED,
        phase=ProductionBatchPhase.REVIEW,
        idempotency_key=f"composition-batch-{uuid4().hex}",
        request_fingerprint="b" * 64,
        actor_id="integration-analyst",
        correlation_id="composition-integration",
    )
    batch_item = EditionProductionBatchItem(
        batch_id=batch.id,
        subject_id=subject.id,
        production_run_id=run.id,
        position=1,
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.edition_production_batches.add(batch)
        await uow._session.flush()
        await uow.edition_production_batch_items.append_many((batch_item,))
        await uow.commit()
    return _PinnedWorld(edition, subject, run, artifact, batch)


async def _persist_extra_subject(uow_factory: UnitOfWorkFactory, edition: Edition) -> _PinnedWorld:
    subject = Subject(
        edition_id=edition.id,
        title="Second composition integration subject",
        slug=f"composition-{uuid4().hex}",
        tlp=TLP.GREEN,
        version=1,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        pipeline_generation=2,
    )
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash="d" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status=ProductionBatchStatus.COMPLETED,
        phase=ProductionBatchPhase.REVIEW,
        idempotency_key=f"composition-batch-{uuid4().hex}",
        request_fingerprint="e" * 64,
        actor_id="integration-analyst",
        correlation_id="composition-integration",
    )
    batch_item = EditionProductionBatchItem(
        batch_id=batch.id,
        subject_id=subject.id,
        production_run_id=run.id,
        position=1,
    )
    async with uow_factory() as uow:
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.edition_production_batches.add(batch)
        await uow._session.flush()
        await uow.edition_production_batch_items.append_many((batch_item,))
        await uow.commit()
    return _PinnedWorld(edition, subject, run, artifact, batch)


def _included(world: _PinnedWorld, *, position: int = 1) -> EditionCompositionEntry:
    return EditionCompositionEntry(
        subject_id=world.subject.id,
        disposition=CompositionDisposition.INCLUDED,
        position=position,
        production_run_id=world.run.id,
        pipeline_generation=world.run.pipeline_generation,
        artifact_id=world.artifact.id,
        artifact_version=world.artifact.version,
        artifact_input_hash=world.artifact.input_hash,
    )


@pytest.mark.asyncio
async def test_postgres_composition_persists_provenance_idempotency_and_append_only_events(
    uow_factory: UnitOfWorkFactory,
) -> None:
    world = await _persist_pinned_world(uow_factory)
    service = EditionCompositionService(uow_factory)
    entry = _included(world)
    created = await service.replace_entries(
        world.edition.id,
        (entry,),
        expected_version=0,
        idempotency_key="compose-1",
        actor_id="analyst-a",
        correlation_id="corr-a",
    )
    replay = await service.replace_entries(
        world.edition.id,
        (_included(world),),
        expected_version=0,
        idempotency_key="compose-1",
        actor_id="analyst-b",
        correlation_id="corr-b",
    )
    assert replay == created
    assert created.version == 1
    assert created.entries[0].provenance_batch_id == world.batch.id

    with pytest.raises(CompositionIdempotencyConflict):
        await service.replace_entries(
            world.edition.id,
            (replace_document_hash(_included(world)),),
            expected_version=0,
            idempotency_key="compose-1",
            actor_id="analyst-a",
            correlation_id="corr-c",
        )

    read = await service.get(world.edition.id)
    assert read.axes.production_validity is True
    assert read.axes.discovery_update_available is None
    assert read.axes.composition_eligibility is True
    assert read.composition is not None
    assert read.composition.entries[0].preparation_status.value == "ready"

    async def insert_duplicate() -> None:
        async with uow_factory() as uow:
            duplicate = EditionCompositionEntryRow(
                id=uuid4(),
                composition_id=created.id,
                subject_id=world.subject.id,
                disposition="deferred",
                position=None,
                production_run_id=None,
                pipeline_generation=None,
                artifact_id=None,
                artifact_version=None,
                artifact_input_hash=None,
                provenance_batch_id=None,
                review_decision_id=None,
                document_sha256=None,
                preparation_status="unassessed",
            )
            uow._session.add(duplicate)
            await uow._session.flush()

    with pytest.raises(IntegrityError):
        await insert_duplicate()

    async def mutate_event() -> None:
        async with uow_factory() as uow:
            await uow._session.execute(
                update(EditionCompositionEventRow)
                .where(EditionCompositionEventRow.composition_id == created.id)
                .values(actor_id="tampered")
            )
            await uow.commit()

    with pytest.raises(DBAPIError):
        await mutate_event()


@pytest.mark.asyncio
async def test_postgres_serializes_two_operators_and_rejects_stale_expected_version(
    uow_factory: UnitOfWorkFactory,
) -> None:
    world = await _persist_pinned_world(uow_factory)
    second_world = await _persist_extra_subject(uow_factory, world.edition)
    service = EditionCompositionService(uow_factory)

    await service.replace_entries(
        world.edition.id,
        (_included(world, position=1), _included(second_world, position=2)),
        expected_version=0,
        idempotency_key="initial-order",
        actor_id="analyst-initial",
        correlation_id="corr-initial",
    )

    async def command(key: str, actor: str, ordered_subjects: tuple[UUID, UUID]):
        return await service.reorder(
            world.edition.id,
            ordered_subjects,
            expected_version=1,
            idempotency_key=key,
            actor_id=actor,
            correlation_id=f"corr-{actor}",
        )

    results = await asyncio.gather(
        command("operator-a", "analyst-a", (world.subject.id, second_world.subject.id)),
        command("operator-b", "analyst-b", (second_world.subject.id, world.subject.id)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CompositionVersionConflict) for result in results) == 1
    successes = [result for result in results if not isinstance(result, BaseException)]
    assert len(successes) == 1
    assert successes[0].version == 2


@pytest.mark.asyncio
async def test_postgres_rejects_artifact_from_another_edition_and_detects_later_change(
    uow_factory: UnitOfWorkFactory,
) -> None:
    world = await _persist_pinned_world(uow_factory)
    other_world = await _persist_pinned_world(uow_factory, country_code="DE")
    service = EditionCompositionService(uow_factory)
    wrong_pin = EditionCompositionEntry(
        subject_id=world.subject.id,
        disposition=CompositionDisposition.INCLUDED,
        position=1,
        production_run_id=other_world.run.id,
        pipeline_generation=other_world.run.pipeline_generation,
        artifact_id=other_world.artifact.id,
        artifact_version=other_world.artifact.version,
        artifact_input_hash=other_world.artifact.input_hash,
    )
    with pytest.raises(CompositionReferenceError) as error:
        await service.include(
            world.edition.id,
            wrong_pin,
            expected_version=0,
            idempotency_key="foreign-artifact",
            actor_id="analyst",
            correlation_id="corr-foreign",
        )
    assert error.value.reason == "production_run_not_in_edition"

    await service.include(
        world.edition.id,
        _included(world),
        expected_version=0,
        idempotency_key="valid-artifact",
        actor_id="analyst",
        correlation_id="corr-valid",
    )
    # The first read observes a verified pin. A subsequent DB change becomes a
    # validation reason without silently moving the composition to another pin.
    before = await service.get(world.edition.id)
    assert before.axes.production_validity is True
    async with uow_factory() as uow:
        await uow._session.execute(
            update(ProductionArtifactRow)
            .where(ProductionArtifactRow.id == world.artifact.id)
            .values(status="stale")
        )
        await uow.commit()
    after = await service.validate(world.edition.id)
    assert CompositionValidationCode.ARTIFACT_NOT_VERIFIED in {
        reason.code for reason in after.reasons
    }
    assert after.composition is not None
    assert after.composition.entries[0].artifact_id == world.artifact.id


@pytest.mark.asyncio
async def test_postgres_rejects_review_decision_for_another_artifact_version(
    uow_factory: UnitOfWorkFactory,
) -> None:
    world = await _persist_pinned_world(uow_factory)
    old_decision = PublicationReviewDecision(
        edition_id=world.edition.id,
        subject_id=world.subject.id,
        production_run_id=world.run.id,
        pipeline_generation=world.run.pipeline_generation,
        document_artifact_id=world.artifact.id,
        document_artifact_version=world.artifact.version,
        document_input_hash=world.artifact.input_hash,
        decision=PublicationDecision.INCLUDE,
        actor_id="integration-analyst",
    )
    newer_artifact = ProductionArtifact(
        production_run_id=world.run.id,
        subject_id=world.subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=2,
        input_hash="f" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    async with uow_factory() as uow:
        await uow.publication_review_decisions.append(old_decision)
        await uow.production_artifacts.append(newer_artifact)
        await uow.commit()

    stale_decision_entry = EditionCompositionEntry(
        subject_id=world.subject.id,
        disposition=CompositionDisposition.INCLUDED,
        position=1,
        production_run_id=world.run.id,
        pipeline_generation=world.run.pipeline_generation,
        artifact_id=newer_artifact.id,
        artifact_version=newer_artifact.version,
        artifact_input_hash=newer_artifact.input_hash,
        review_decision_id=old_decision.id,
    )
    with pytest.raises(CompositionReferenceError) as error:
        await EditionCompositionService(uow_factory).include(
            world.edition.id,
            stale_decision_entry,
            expected_version=0,
            idempotency_key="new-version-old-review",
            actor_id="integration-analyst",
            correlation_id="composition-stale-review",
        )

    assert error.value.reason == "review_decision_identity_mismatch"


def replace_document_hash(entry: EditionCompositionEntry) -> EditionCompositionEntry:
    return EditionCompositionEntry(
        subject_id=entry.subject_id,
        disposition=entry.disposition,
        position=entry.position,
        production_run_id=entry.production_run_id,
        pipeline_generation=entry.pipeline_generation,
        artifact_id=entry.artifact_id,
        artifact_version=entry.artifact_version,
        artifact_input_hash=entry.artifact_input_hash,
        document_sha256="c" * 64,
    )

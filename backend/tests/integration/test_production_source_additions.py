"""PostgreSQL persistence proof for the append-only addition journal."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_references import (
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.production_source_additions import (
    SourceAdditionJournalEntryV1,
    SourceAdditionOrigin,
    SourceAdditionState,
    SourceAdditionV1,
    source_addition_source_key,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_source_addition_repository_round_trips_immutable_versions(
    uow_factory: UnitOfWorkFactory,
) -> None:
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Source addition repository test",
        slug=f"source-addition-{uuid4().hex[:12]}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.commit()

    request_id = uuid4()
    addition = SourceAdditionV1(
        addition_id=uuid4(),
        request_id=request_id,
        production_run_id=run.id,
        subject_id=subject.id,
        state=SourceAdditionState.CANDIDATE,
        origin=SourceAdditionOrigin.OPERATOR_URL,
        canonical_url="https://example.test/report",
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
    )
    selected = replace(
        addition,
        state=SourceAdditionState.SELECTED,
        selection_criterion="Relevant to the current case",
        selection_criterion_version="criterion-v1",
        selected_by="analyst@example.test",
    )
    async with uow_factory() as uow:
        await uow.production_source_additions.append(
            SourceAdditionJournalEntryV1(addition=addition, version=1)
        )
        await uow.production_source_additions.append(
            SourceAdditionJournalEntryV1(addition=selected, version=2)
        )
        await uow.commit()

    async with uow_factory() as uow:
        latest = await uow.production_source_additions.get_latest_for_identity(
            run.id, request_id, source_addition_source_key(addition.canonical_url)
        )
        listed = await uow.production_source_additions.list_latest_for_request(run.id, request_id)

    assert latest == SourceAdditionJournalEntryV1(addition=selected, version=2)
    assert listed == [latest]


@pytest.mark.asyncio
async def test_source_addition_and_promotion_artifact_rollback_in_one_postgres_uow(
    uow_factory: UnitOfWorkFactory,
) -> None:
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Source addition transaction test",
        slug=f"source-addition-rollback-{uuid4().hex[:12]}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.commit()
    async with uow_factory() as uow:
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.commit()

    request_id = uuid4()
    addition = SourceAdditionV1(
        addition_id=uuid4(),
        request_id=request_id,
        production_run_id=run.id,
        subject_id=subject.id,
        state=SourceAdditionState.CANDIDATE,
        origin=SourceAdditionOrigin.OPERATOR_URL,
        canonical_url="https://example.test/nanodump",
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
    )
    promoted_references = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="a" * 64,
        metadata={"source_additions_hash": "b" * 64},
    )
    with pytest.raises(RuntimeError, match="revision failed after promotion"):
        async with uow_factory() as uow:
            await uow.production_source_additions.append(
                SourceAdditionJournalEntryV1(addition=addition, version=1)
            )
            await uow.production_artifacts.append(promoted_references)
            raise RuntimeError("revision failed after promotion")

    async with uow_factory() as uow:
        latest = await uow.production_source_additions.get_latest_for_identity(
            run.id, request_id, source_addition_source_key(addition.canonical_url)
        )
        artifact = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.REFERENCES.value
        )

    assert latest is None
    assert artifact is None

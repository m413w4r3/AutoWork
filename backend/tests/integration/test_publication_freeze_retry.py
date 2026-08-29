"""PostgreSQL proof that publication freeze wins a concurrent user retry."""

from __future__ import annotations

import asyncio
from datetime import date
from time import monotonic
from uuid import uuid4

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import PublicationManifestEntryV1, PublicationManifestV1
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_freeze_blocks_concurrent_retry_after_edition_lock_is_released(
    uow_factory: UnitOfWorkFactory,
) -> None:
    edition = Edition(
        country="Freeze Test",
        country_code="FT",
        period_start=date(2099, 8, 1),
        period_end=date(2099, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="test",
        status=EditionStatus.REVIEW,
    )
    subject = Subject(
        external_id=f"freeze-{uuid4().hex}",
        slug=f"freeze-{uuid4().hex}",
        tlp=TLP.GREEN,
    )
    run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=SubjectProductionStatus.FAILED,
        current_stage=SubjectProductionStage.SOURCES,
        error_code="production_failed",
        error_message="production failed",
    )
    artifact_blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256="a" * 64,
            size=1,
            mime_type="application/json",
            logical_bucket="test-publication",
        )
    )
    manifest_blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256="b" * 64,
            size=1,
            mime_type="application/json",
            logical_bucket="test-manifests",
        )
    )
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash="c" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=artifact_blob.id,
    )
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status="running",
        phase=ProductionBatchPhase.REVIEW,
    )
    item = EditionProductionBatchItem(
        batch_id=batch.id,
        subject_id=subject.id,
        production_run_id=run.id,
        position=1,
    )

    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.blobs.add(artifact_blob)
        await uow.blobs.add(manifest_blob)
        await uow.edition_production_batches.add(batch)
        await uow.subject_production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.edition_production_batch_items.append_many((item,))
        await uow.commit()

    manifest = PublicationManifestV1.create(
        edition_id=edition.id,
        edition_version=edition.version,
        batch_id=batch.id,
        created_by="analyst",
        entries=(
            PublicationManifestEntryV1(
                position=1,
                subject_id=subject.id,
                production_run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                document_artifact_id=artifact.id,
                document_artifact_version=artifact.version,
                document_input_hash=artifact.input_hash,
            ),
        ),
        exclusions=(),
    )
    async with uow_factory() as uow:
        await uow.publication_manifests.add(manifest, manifest_blob.id)
        await uow.publication_manifest_entries.append_many(manifest.id, manifest.entries)
        await uow.commit()

    edition_locked = asyncio.Event()

    async def freeze() -> None:
        async with uow_factory() as uow:
            locked = await uow.editions.get_for_update(edition.id)
            assert locked is not None
            before = locked.snapshot()
            locked.transition(EditionStatus.ASSEMBLING)
            assert await uow.editions.update(locked, int(before["version"]))
            edition_locked.set()
            # Let the retry session reach and wait on the same row lock.
            await asyncio.sleep(0.15)
            await uow.commit()

    async def retry() -> tuple[str, float]:
        await edition_locked.wait()
        started = monotonic()
        with pytest.raises(ValueError, match="edition_frozen_for_publication"):
            await SubjectProductionService(uow_factory).retry_from_stage(
                run.id, SubjectProductionStage.SOURCES
            )
        return "edition_frozen_for_publication", monotonic() - started

    freeze_task = asyncio.create_task(freeze())
    retry_task = asyncio.create_task(retry())
    _, retry_result = await asyncio.gather(freeze_task, retry_task)
    result = retry_result
    assert result[0] == "edition_frozen_for_publication"
    assert result[1] >= 0.10

    async with uow_factory() as uow:
        persisted_edition = await uow.editions.get(edition.id)
        persisted_run = await uow.subject_production_runs.get(run.id)
        persisted_artifact = await uow.production_artifacts.get(artifact.id)
        persisted_manifest = await uow.publication_manifests.get(manifest.id)

    assert persisted_edition is not None
    assert persisted_edition.status is EditionStatus.ASSEMBLING
    assert persisted_run is not None
    assert persisted_run.pipeline_generation == run.pipeline_generation
    assert persisted_artifact is not None
    assert persisted_artifact.status is ProductionArtifactStatus.VERIFIED
    assert persisted_manifest is not None
    assert persisted_manifest.entries[0].position == 1

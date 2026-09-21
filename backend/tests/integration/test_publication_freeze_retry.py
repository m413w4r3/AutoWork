"""PostgreSQL proof that publication does not freeze the Edition state."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
    PublicationAssemblyError,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
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
    ProductionBatchStatus,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
    production_batch_request_fingerprint,
)
from cti_app.domain.publication import PublicationDocumentV2
from tests.integration.production.support import ProductionScenario

pytestmark = pytest.mark.integration


async def _seed_review_snapshot(
    uow_factory: UnitOfWorkFactory,
    scenario: ProductionScenario,
    store: ProductionArtifactStore,
    *,
    created_at: datetime,
    title: str,
    input_hash: str,
    run_number: int,
) -> tuple[EditionProductionBatch, ProductionRun, ProductionArtifact]:
    document = PublicationDocumentV2(
        schema_version="2",
        title=title,
        timeline=(),
        synthesis=(),
        indicators=(),
        sources=(),
        uncertainties=(),
    )
    document_blob_id, _ = await store.put_canonical_json(
        document.to_json(), bucket="test-publication"
    )
    run = ProductionRun(
        subject_id=scenario.subject.id,
        edition_id=scenario.edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        run_number=run_number,
        pipeline_generation=1,
        created_at=created_at,
        updated_at=created_at,
    )
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=scenario.subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=document_blob_id,
        created_at=created_at,
    )
    batch = EditionProductionBatch(
        edition_id=scenario.edition.id,
        status=ProductionBatchStatus.RUNNING,
        phase=ProductionBatchPhase.REVIEW,
        idempotency_key=(f"publication-freeze:{scenario.edition.id}:{run_number}"),
        request_fingerprint=production_batch_request_fingerprint(
            scenario.edition.id,
            [scenario.subject.id],
        ),
        actor_id="publication-freeze-test",
        correlation_id=f"publication-freeze:{run_number}",
        created_at=created_at,
    )
    item = EditionProductionBatchItem(
        batch_id=batch.id,
        subject_id=scenario.subject.id,
        production_run_id=run.id,
        position=1,
        created_at=created_at,
    )
    async with uow_factory() as uow:
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.edition_production_batches.add(batch)
        await uow.commit()
        await uow.edition_production_batch_items.append_many((item,))
        await uow.commit()
    return batch, run, artifact


async def _publication_scenario(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> ProductionScenario:
    scenario = ProductionScenario(
        uow_factory,
        tmp_path / "publication-blobs",
        {"https://example.test/publication": {"body": "publication"}},
    )
    await scenario.seed()
    return scenario


@pytest.mark.asyncio
async def test_same_version_publication_snapshots_and_releases_are_chronological(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    scenario = await _publication_scenario(uow_factory, tmp_path)
    store = scenario.artifact_store
    first_at = datetime.now(UTC)
    await _seed_review_snapshot(
        uow_factory,
        scenario,
        store,
        created_at=first_at,
        title="Snapshot A",
        input_hash="a" * 64,
        run_number=1,
    )
    publication = EditionPublicationService(uow_factory, store)
    assembly = EditionAssemblyService(uow_factory, store)

    accepted_a = await publication.accept(scenario.edition.id, actor_id="reviewer")
    release_a = await assembly.assemble(accepted_a.manifest.id)

    await _seed_review_snapshot(
        uow_factory,
        scenario,
        store,
        created_at=first_at + timedelta(seconds=1),
        title="Snapshot B",
        input_hash="b" * 64,
        run_number=2,
    )
    accepted_b = await publication.accept(scenario.edition.id, actor_id="reviewer")
    release_b = await assembly.assemble(accepted_b.manifest.id)

    async with uow_factory() as uow:
        latest = await uow.publication_manifests.get_latest_for_edition(scenario.edition.id)
        persisted_a = await uow.publication_manifests.get(accepted_a.manifest.id)
        persisted_b = await uow.publication_manifests.get(accepted_b.manifest.id)
        release_for_a = await uow.edition_releases.get_by_manifest(accepted_a.manifest.id)
        release_for_b = await uow.edition_releases.get_by_manifest(accepted_b.manifest.id)
        edition = await uow.editions.get(scenario.edition.id)

    assert persisted_a is not None and persisted_b is not None
    assert release_for_a is not None and release_for_b is not None
    assert release_a.id == release_for_a.id
    assert release_b.id == release_for_b.id
    assert latest is not None and latest.id == accepted_b.manifest.id
    assert persisted_a.edition_version == persisted_b.edition_version
    assert edition is not None and edition.version == persisted_a.edition_version


@pytest.mark.asyncio
async def test_same_version_pending_snapshot_rejects_changed_production_review_inputs(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    scenario = await _publication_scenario(uow_factory, tmp_path)
    store = scenario.artifact_store
    first_at = datetime.now(UTC)
    await _seed_review_snapshot(
        uow_factory,
        scenario,
        store,
        created_at=first_at,
        title="Pending A",
        input_hash="a" * 64,
        run_number=1,
    )
    publication = EditionPublicationService(uow_factory, store)
    assembly = EditionAssemblyService(uow_factory, store)
    accepted_a = await publication.accept(scenario.edition.id, actor_id="reviewer")
    version = accepted_a.manifest.edition_version

    await _seed_review_snapshot(
        uow_factory,
        scenario,
        store,
        created_at=first_at + timedelta(seconds=1),
        title="Pending B",
        input_hash="b" * 64,
        run_number=2,
    )

    with pytest.raises(PublicationAssemblyError, match="publication_inputs_changed_after_freeze"):
        await assembly.assemble(accepted_a.manifest.id)

    async with uow_factory() as uow:
        release = await uow.edition_releases.get_by_manifest(accepted_a.manifest.id)
        edition = await uow.editions.get(scenario.edition.id)

    assert release is None
    assert edition is not None and edition.version == version


@pytest.mark.asyncio
async def test_publication_lock_keeps_the_edition_open_for_a_concurrent_retry(
    uow_factory: UnitOfWorkFactory,
) -> None:
    edition = Edition(
        country="Freeze Test",
        country_code="FT",
        period_start=date(2099, 8, 1),
        period_end=date(2099, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        state=EditionStatus.OPEN,
    )
    subject_created_at = datetime.now(UTC)
    subject = Subject(
        edition_id=edition.id,
        title="Freeze subject",
        slug=f"freeze-{uuid4().hex}",
        tlp=TLP.GREEN,
        version=1,
        created_at=subject_created_at,
        updated_at=subject_created_at,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.FAILED,
        current_stage=ProductionStage.SOURCES,
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
        await uow.production_runs.add(run)
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
            assert locked.state is EditionStatus.OPEN
            edition_locked.set()
            # Let the second session reach and wait on the same row lock.
            await asyncio.sleep(0.15)
            await uow.commit()

    async def retry() -> tuple[str, float]:
        await edition_locked.wait()
        started = monotonic()
        async with uow_factory() as uow:
            retried = await uow.editions.get_for_update(edition.id)
            assert retried is not None
            assert retried.state is EditionStatus.OPEN
        return "edition_remained_open", monotonic() - started

    freeze_task = asyncio.create_task(freeze())
    retry_task = asyncio.create_task(retry())
    _, retry_result = await asyncio.gather(freeze_task, retry_task)
    result = retry_result
    assert result[0] == "edition_remained_open"
    assert result[1] >= 0.10

    async with uow_factory() as uow:
        persisted_edition = await uow.editions.get(edition.id)
        persisted_run = await uow.production_runs.get(run.id)
        persisted_artifact = await uow.production_artifacts.get(artifact.id)
        persisted_manifest = await uow.publication_manifests.get(manifest.id)

    assert persisted_edition is not None
    assert persisted_edition.state is EditionStatus.OPEN
    assert persisted_run is not None
    assert persisted_run.pipeline_generation == run.pipeline_generation
    assert persisted_artifact is not None
    assert persisted_artifact.status is ProductionArtifactStatus.VERIFIED
    assert persisted_manifest is not None
    assert persisted_manifest.entries[0].position == 1

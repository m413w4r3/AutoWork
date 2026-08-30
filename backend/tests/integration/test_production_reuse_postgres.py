"""PostgreSQL proof for canonical cross-run production artifact reuse."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionReuseInvalidation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore

pytestmark = pytest.mark.integration


def _edition() -> Edition:
    return Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_articles=1,
        source_profile="test",
    )


async def _seed_computed_run(
    uow_factory: UnitOfWorkFactory,
    store: ProductionArtifactStore,
    *,
    edition: Edition,
    subject: Subject,
    created_at: datetime,
) -> tuple[SubjectProductionRun, dict[ProductionArtifactStage, ProductionArtifact]]:
    # The logical edition repository may replace a freshly generated ID with
    # an existing edition's ID, so persist these parents before constructing
    # the run that references the final edition ID.
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.commit()

    run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        created_at=created_at,
        updated_at=created_at,
    )
    refs_blobs = await store.store_stage_payloads(
        raw="references raw",
        canonical={"stage": "references"},
    )
    extraction_blobs = await store.store_stage_payloads(
        raw="extraction raw",
        canonical={"stage": "extraction"},
    )
    synthesis_blobs = await store.store_stage_payloads(
        raw="synthesis raw",
        rendered="synthesis rendered",
    )
    blobs = {
        ProductionArtifactStage.REFERENCES: refs_blobs,
        ProductionArtifactStage.EXTRACTION: extraction_blobs,
        ProductionArtifactStage.SYNTHESIS: synthesis_blobs,
    }
    hashes = {
        ProductionArtifactStage.REFERENCES: "a" * 64,
        ProductionArtifactStage.EXTRACTION: "b" * 64,
        ProductionArtifactStage.SYNTHESIS: "c" * 64,
    }
    artifacts = {
        stage: ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=stage,
            version=1,
            input_hash=hashes[stage],
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=payloads[0],
            canonical_blob_id=payloads[1],
            rendered_blob_id=payloads[2],
            created_at=created_at,
        )
        for stage, payloads in blobs.items()
    }
    async with uow_factory() as uow:
        await uow.subject_production_runs.add(run)
        for artifact in artifacts.values():
            await uow.production_artifacts.append(artifact)
        await uow.commit()
    return run, artifacts


@pytest.mark.asyncio
async def test_postgres_run_b_reuses_all_costly_artifacts_from_run_a(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition = _edition()
    subject = Subject(
        external_id=f"SUBJ-REUSE-{uuid4()}",
        slug=f"reuse-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    created_at = datetime.now(UTC) - timedelta(minutes=1)
    source_run, source_artifacts = await _seed_computed_run(
        uow_factory,
        store,
        edition=edition,
        subject=subject,
        created_at=created_at,
    )
    target_run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        run_number=2,
        status=SubjectProductionStatus.RUNNING,
        current_stage=SubjectProductionStage.REFERENCES,
    )
    async with uow_factory() as uow:
        await uow.subject_production_runs.add(target_run)
        await uow.commit()

    service = ProductionArtifactReuseService(uow_factory, store)
    for stage, source in source_artifacts.items():
        result = await service.find_or_reuse(
            run=target_run,
            stage=stage,
            input_hash=source.input_hash,
        )
        assert result is not None
        assert result.reused is True
        assert result.artifact.production_run_id == target_run.id
        assert result.artifact.production_run_id != source_run.id
        assert result.artifact.reused_from_artifact_id == source.id
        assert result.artifact.raw_blob_id == source.raw_blob_id
        assert result.artifact.canonical_blob_id == source.canonical_blob_id
        assert result.artifact.rendered_blob_id == source.rendered_blob_id

    async with uow_factory() as uow:
        source_rows = await uow.production_artifacts.list_for_run(source_run.id)
        target_rows = await uow.production_artifacts.list_for_run(target_run.id)
    assert {row.id for row in source_rows} == {
        artifact.id for artifact in source_artifacts.values()
    }
    assert {row.stage for row in target_rows} == set(source_artifacts)
    assert all(row.reused_from_artifact_id is not None for row in target_rows)


@pytest.mark.asyncio
async def test_postgres_invalidation_from_extraction_preserves_refs_only(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition = _edition()
    subject = Subject(
        external_id=f"SUBJ-INVALIDATE-{uuid4()}",
        slug=f"invalidate-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    source_run, source_artifacts = await _seed_computed_run(
        uow_factory,
        store,
        edition=edition,
        subject=subject,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    occurred_at = datetime.now(UTC) - timedelta(minutes=1)
    async with uow_factory() as uow:
        await uow.production_reuse_invalidations.add(
            ProductionReuseInvalidation(
                edition_id=edition.id,
                subject_id=subject.id,
                from_stage=SubjectProductionStage.EXTRACTION,
                actor_id="operator",
                correlation_id=str(uuid4()),
                occurred_at=occurred_at,
            )
        )
        target_run = SubjectProductionRun(
            subject_id=subject.id,
            edition_id=edition.id,
            run_number=2,
            status=SubjectProductionStatus.RUNNING,
            current_stage=SubjectProductionStage.REFERENCES,
        )
        await uow.subject_production_runs.add(target_run)
        await uow.commit()

    service = ProductionArtifactReuseService(uow_factory, store)
    references = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.REFERENCES,
        input_hash=source_artifacts[ProductionArtifactStage.REFERENCES].input_hash,
    )
    extraction = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.EXTRACTION,
        input_hash=source_artifacts[ProductionArtifactStage.EXTRACTION].input_hash,
    )
    synthesis = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.SYNTHESIS,
        input_hash=source_artifacts[ProductionArtifactStage.SYNTHESIS].input_hash,
    )

    assert references is not None and references.reused
    assert extraction is None
    assert synthesis is None
    async with uow_factory() as uow:
        old_rows = await uow.production_artifacts.list_for_run(source_run.id)
    assert len(old_rows) == 3

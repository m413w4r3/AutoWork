"""PostgreSQL regression coverage for production artifact version allocation."""

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    reference_report_from_json,
    technical_extraction_from_json,
    validate_synthesis,
)
from cti_app.application.production_state import ProductionStateService
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore

pytestmark = pytest.mark.integration


def _artifact(
    run: SubjectProductionRun, stage: ProductionArtifactStage, version: int
) -> ProductionArtifact:
    return ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=stage,
        version=version,
        input_hash=f"{version:x}" * 64,
    )


@pytest.mark.asyncio
async def test_stale_artifacts_are_replaced_with_monotonic_versions_in_postgres(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Exercise ``uq_run_stage_version`` over two real stale-and-replace cycles."""
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_major_articles=1,
        target_briefs=1,
        source_profile="test",
    )
    subject = Subject(external_id="SUBJ-ARTIFACT-VERSIONS", slug="artifact-versions", tlp=TLP.AMBER)
    run = SubjectProductionRun(
        subject_id=subject.id, edition_id=edition.id, profile=ProductionProfile.BRIEF_AUTO
    )

    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.subject_production_runs.add(run)
        for stage in (ProductionArtifactStage.REFERENCES, ProductionArtifactStage.EXTRACTION):
            await uow.production_artifacts.append(_artifact(run, stage, 1))
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.production_artifacts.mark_from_stage_stale(run.id, "references") == [
            "references",
            "extraction",
            "synthesis",
            "brief",
        ]
        for stage in (ProductionArtifactStage.REFERENCES, ProductionArtifactStage.EXTRACTION):
            await uow.production_artifacts.append(_artifact(run, stage, 2))
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.production_artifacts.mark_from_stage_stale(run.id, "references") == [
            "references",
            "extraction",
            "synthesis",
            "brief",
        ]
        for stage in (ProductionArtifactStage.REFERENCES, ProductionArtifactStage.EXTRACTION):
            await uow.production_artifacts.append(_artifact(run, stage, 3))
        await uow.commit()

    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(run.id)
    assert [(item.stage.value, item.version, item.status.value) for item in artifacts] == [
        ("extraction", 1, "stale"),
        ("extraction", 2, "stale"),
        ("extraction", 3, "verified"),
        ("references", 1, "stale"),
        ("references", 2, "stale"),
        ("references", 3, "verified"),
    ]


@pytest.mark.asyncio
async def test_production_state_round_trip_uses_real_postgres_and_blob_catalog(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """A portable checkpoint survives a closed UoW and remains consumer-transparent."""
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_major_articles=1,
        target_briefs=1,
        source_profile="test",
    )
    source = Subject(external_id="SUBJ-STATE-A", slug="state-a", tlp=TLP.AMBER)
    target = Subject(external_id="SUBJ-STATE-B", slug="state-b", tlp=TLP.AMBER)
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    refs: dict[str, Any] = {
        "sources": [
            {
                "id": "S1",
                "title": "Source",
                "url": "https://example.test/source",
                "canonical_url": "https://example.test/source",
            }
        ],
        "events": [],
    }
    extraction: dict[str, Any] = {"items": [], "uncertainties": []}
    synthesis = "Fait [S1]"
    run = SubjectProductionRun(
        subject_id=source.id, edition_id=edition.id, profile=ProductionProfile.BRIEF_AUTO
    )
    run.start_running()
    run.current_stage = SubjectProductionStage.ASSEMBLY
    run.mark_needs_review(code="seed", message="seed")
    ref_blob = await store.store_stage_payloads(canonical=refs)
    extraction_blob = await store.store_stage_payloads(canonical=extraction)
    synthesis_blob = await store.store_stage_payloads(rendered=synthesis)
    artifacts = (
        (ProductionArtifactStage.REFERENCES, ref_blob[1], None),
        (ProductionArtifactStage.EXTRACTION, extraction_blob[1], None),
        (ProductionArtifactStage.SYNTHESIS, None, synthesis_blob[2]),
    )
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(source)
        await uow.subjects.add(target)
        await uow.subject_production_runs.add(run)
        for stage, canonical_blob_id, rendered_blob_id in artifacts:
            await uow.production_artifacts.append(
                ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=source.id,
                    stage=stage,
                    version=1,
                    input_hash="a" * 64,
                    status=ProductionArtifactStatus.VERIFIED,
                    canonical_blob_id=canonical_blob_id,
                    rendered_blob_id=rendered_blob_id,
                )
            )
        await uow.commit()

    service = ProductionStateService(uow_factory, store)
    snapshot = await service.export_state(subject_id=source.id, subject_title="Source")
    result = await service.import_state(
        subject_id=target.id, edition_id=edition.id, payload=snapshot.model_dump(mode="json")
    )

    async with uow_factory() as uow:
        imported = await uow.subject_production_runs.get(result.run_id)
        assert imported is not None
        assert imported.status.value == "needs_review"
        assert imported.current_stage.value == "assembly"
        imported_artifacts = [
            await uow.production_artifacts.get_current(imported.id, stage.value)
            for stage in (
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
            )
        ]
    assert all(artifact is not None for artifact in imported_artifacts)
    imported_refs_artifact, imported_extraction_artifact, imported_synthesis_artifact = (
        artifact for artifact in imported_artifacts if artifact is not None
    )
    assert imported_refs_artifact.canonical_blob_id is not None
    assert imported_extraction_artifact.canonical_blob_id is not None
    assert imported_synthesis_artifact.rendered_blob_id is not None
    imported_refs = await store.read_json(imported_refs_artifact.canonical_blob_id)
    imported_extraction = await store.read_json(imported_extraction_artifact.canonical_blob_id)
    imported_synthesis = await store.read_text(imported_synthesis_artifact.rendered_blob_id)
    assert reference_report_from_json(imported_refs) == reference_report_from_json(refs)
    assert technical_extraction_from_json(imported_extraction) == technical_extraction_from_json(
        extraction
    )
    assert validate_synthesis(
        imported_synthesis,
        reference_report_from_json(refs),
        technical_extraction_from_json(extraction),
    ).usable


@pytest.mark.asyncio
async def test_run_number_allocation_is_serialized_in_postgres(
    uow_factory: UnitOfWorkFactory,
) -> None:
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_major_articles=1,
        target_briefs=1,
        source_profile="test",
    )
    subject = Subject(external_id="SUBJ-RUN-LOCK", slug="run-lock", tlp=TLP.AMBER)
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.commit()

    async def create_run() -> int:
        async with uow_factory() as uow:
            number = await uow.subject_production_runs.allocate_next_run_number(subject.id)
            await uow.subject_production_runs.add(
                SubjectProductionRun(
                    subject_id=subject.id,
                    edition_id=edition.id,
                    profile=ProductionProfile.BRIEF_AUTO,
                    run_number=number,
                )
            )
            await uow.commit()
            return number

    assert sorted(await asyncio.gather(create_run(), create_run())) == [1, 2]

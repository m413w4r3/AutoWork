"""PostgreSQL regression coverage for production artifact version allocation."""

from datetime import date

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionProfile,
    SubjectProductionRun,
)

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

"""PostgreSQL coverage for invalidation driven by the canonical pipeline graph."""

from __future__ import annotations

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
    ProductionArtifactStatus,
    ProductionRun,
    ProductionStage,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_artifact_invalidation_uses_pipeline_dependencies(
    uow_factory: UnitOfWorkFactory,
) -> None:
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    cases: tuple[tuple[str, str, set[str]], ...] = (
        (
            "artifact",
            ProductionArtifactStage.REFERENCES.value,
            {"extraction", "synthesis", "publication"},
        ),
        (
            "artifact",
            ProductionArtifactStage.EXTRACTION.value,
            {"synthesis", "publication"},
        ),
        ("artifact", ProductionArtifactStage.SYNTHESIS.value, {"publication"}),
        ("artifact", ProductionArtifactStage.PUBLICATION.value, set()),
        (
            "pipeline",
            ProductionStage.SOURCES.value,
            {stage.value for stage in ProductionArtifactStage},
        ),
        (
            "pipeline",
            ProductionStage.EXTRACTION.value,
            {"extraction", "synthesis", "publication"},
        ),
        ("pipeline", ProductionStage.ASSEMBLY.value, {"publication"}),
    )
    runs: list[ProductionRun] = []

    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        for _kind, _stage, _expected in cases:
            subject = Subject(
                edition_id=edition.id,
                title="Pipeline invalidation",
                slug=f"pipeline-invalidation-{uuid4().hex}",
                tlp=TLP.AMBER,
            )
            await uow.subjects.add(subject)
            run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
            await uow.production_runs.add(run)
            runs.append(run)
            for artifact_stage in ProductionArtifactStage:
                await uow.production_artifacts.append(
                    ProductionArtifact(
                        production_run_id=run.id,
                        subject_id=subject.id,
                        stage=artifact_stage,
                        version=1,
                        input_hash="a" * 64,
                    )
                )
        await uow.commit()

    async with uow_factory() as uow:
        for run, (kind, stage, _expected) in zip(runs, cases, strict=True):
            if kind == "artifact":
                await uow.production_artifacts.mark_downstream_stale(run.id, stage)
            else:
                await uow.production_artifacts.mark_from_stage_stale(run.id, stage)
        await uow.commit()

    async with uow_factory() as uow:
        for run, (_kind, _stage, expected_stale) in zip(runs, cases, strict=True):
            artifacts = await uow.production_artifacts.list_for_run(run.id)
            actual_stale = {
                artifact.stage.value
                for artifact in artifacts
                if artifact.status is ProductionArtifactStatus.STALE
            }
            assert actual_stale == expected_stale

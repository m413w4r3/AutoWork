"""PostgreSQL invariants for selective LOT 33 repair materialization."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    SubjectProductionRun,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_lot33_postgres_selective_versions_keep_current_and_audit_distinct(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """IOC INCLUDE/EXCLUDE creates only new projections/publications."""
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_articles=1,
        source_profile="lot33",
        status=EditionStatus.REVIEW,
    )
    subject = Subject(
        external_id=f"LOT33-{uuid4().hex}",
        slug=f"lot33-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    run = SubjectProductionRun(subject_id=subject.id, edition_id=edition.id)

    def artifact(
        stage: ProductionArtifactStage, version: int, **metadata: object
    ) -> ProductionArtifact:
        return ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=stage,
            version=version,
            input_hash=(f"{version:x}" * 64)[:64],
            metadata=dict(metadata),
        )

    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.subject_production_runs.add(run)
        for stage in ProductionArtifactStage:
            await uow.production_artifacts.append(artifact(stage, 1))
        await uow.commit()

    async with uow_factory() as uow:
        base_extraction = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.EXTRACTION.value
        )
    assert base_extraction is not None

    decision_one = ProductionRepairDecision(
        edition_id=edition.id,
        subject_id=subject.id,
        production_run_id=run.id,
        observed_artifact_id=base_extraction.id,
        observed_pipeline_generation=run.pipeline_generation,
        repair_key="a" * 64,
        issue_kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        action=ProductionRepairAction.INCLUDE,
        actor_id="analyst",
    )
    audit_v2 = {
        "planner_version": "33.1",
        "impact_kind": "publication_only",
        "affected_outputs": ["checkpoint", "extraction", "publication"],
        "model_call_required": False,
        "decision_ids": [str(decision_one.id)],
        "base_extraction_artifact_id": str(base_extraction.id),
        "result_extraction_artifact_id": None,
        "reused_synthesis_artifact_id": str(uuid4()),
        "result_publication_artifact_id": None,
    }
    extraction_v2 = artifact(
        ProductionArtifactStage.EXTRACTION, 2, repair_materialization=audit_v2
    )
    publication_v2 = artifact(ProductionArtifactStage.PUBLICATION, 2)
    audit_v2["result_extraction_artifact_id"] = str(extraction_v2.id)
    audit_v2["result_publication_artifact_id"] = str(publication_v2.id)
    async with uow_factory() as uow:
        await uow.production_repair_decisions.append(decision_one)
        await uow.production_artifacts.mark_stages_stale(run.id, {"publication"})
        await uow.production_artifacts.append(extraction_v2)
        await uow.production_artifacts.append(publication_v2)
        await uow.commit()

    decision_two = ProductionRepairDecision(
        edition_id=edition.id,
        subject_id=subject.id,
        production_run_id=run.id,
        observed_artifact_id=extraction_v2.id,
        observed_pipeline_generation=run.pipeline_generation,
        repair_key=decision_one.repair_key,
        issue_kind=decision_one.issue_kind,
        action=ProductionRepairAction.EXCLUDE,
        actor_id="reviewer",
    )
    audit_v3 = {
        **audit_v2,
        "decision_ids": [str(decision_two.id)],
        "result_extraction_artifact_id": None,
        "result_publication_artifact_id": None,
    }
    extraction_v3 = artifact(
        ProductionArtifactStage.EXTRACTION, 3, repair_materialization=audit_v3
    )
    publication_v3 = artifact(ProductionArtifactStage.PUBLICATION, 3)
    audit_v3["result_extraction_artifact_id"] = str(extraction_v3.id)
    audit_v3["result_publication_artifact_id"] = str(publication_v3.id)
    async with uow_factory() as uow:
        await uow.production_repair_decisions.append(decision_two)
        await uow.production_artifacts.mark_stages_stale(run.id, {"publication"})
        await uow.production_artifacts.append(extraction_v3)
        await uow.production_artifacts.append(publication_v3)
        await uow.commit()

    async with uow_factory() as uow:
        current_extraction = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.EXTRACTION.value
        )
        current_synthesis = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.SYNTHESIS.value
        )
        current_publication = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.PUBLICATION.value
        )
        rows = await uow.production_artifacts.list_for_run(run.id)
        decisions = await uow.production_repair_decisions.list_for_edition(
            edition.id, subject.id
        )
        effective = await uow.production_repair_decisions.effective_decisions(
            edition.id, subject.id
        )

    assert current_extraction is not None and current_extraction.version == 3
    assert current_synthesis is not None and current_synthesis.version == 1
    assert current_publication is not None and current_publication.version == 3
    assert len(decisions) == 2
    assert effective == (decision_two,)
    assert current_extraction.metadata["repair_materialization"]["decision_ids"] == [
        str(decision_two.id)
    ]
    assert len({(row.stage, row.version) for row in rows}) == len(rows)
    assert {
        row.version for row in rows if row.stage is ProductionArtifactStage.EXTRACTION
    } == {1, 2, 3}
    assert {
        row.version for row in rows if row.stage is ProductionArtifactStage.PUBLICATION
    } == {1, 2, 3}
    assert all(
        row.status is ProductionArtifactStatus.STALE
        for row in rows
        if row.version < 3 and row.stage is ProductionArtifactStage.PUBLICATION
    )
    assert all(
        row.status is ProductionArtifactStatus.VERIFIED
        for row in rows
        if row.stage is ProductionArtifactStage.EXTRACTION
    )

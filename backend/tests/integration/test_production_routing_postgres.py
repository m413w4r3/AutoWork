"""PostgreSQL persistence and CAS coverage for publication routing."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from uuid import uuid4

import pytest

from cti_app.application.persistence import (
    ProductionRoutingDecisionConflictError,
    StaleProductionRoutingGenerationError,
    UnitOfWorkFactory,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_hashes,
)
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceProjectionV1,
    RelevanceReasonCode,
)
from cti_app.domain.production_routing import (
    ProductionRoutingArtifactInputV1,
    RoutingStatus,
    decide_publication_routing,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_kind import PublicationKind
from tests.test_production_routing import (
    _SERVICES,
    NOW,
    _extraction,
    _indicator,
    _projection,
)

pytestmark = pytest.mark.integration


def _artifact(
    run: ProductionRun, stage: ProductionArtifactStage, version: int
) -> ProductionArtifact:
    return ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=stage,
        version=version,
        input_hash=f"{version:x}" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )


def _decision(
    *,
    run: ProductionRun,
    extraction: ProductionExtractionV1,
    projection: RelevanceProjectionV1,
    extraction_artifact: ProductionArtifact,
    relevance_artifact: ProductionArtifact,
    now: datetime,
):
    extraction_hash = production_extraction_hashes(extraction)[0]
    return decide_publication_routing(
        production_run_id=run.id,
        subject_id=run.subject_id,
        pipeline_generation=run.pipeline_generation,
        extraction_input=ProductionRoutingArtifactInputV1(
            extraction_artifact, extraction_hash, extraction
        ),
        relevance_input=ProductionRoutingArtifactInputV1(
            relevance_artifact,
            projection.projection_hash,
            projection,
        ),
        legitimate_services=_SERVICES,
        decided_at=now,
    )


@pytest.mark.asyncio
async def test_routing_postgres_cas_replay_conflict_and_requalification_history(
    uow_factory: UnitOfWorkFactory,
) -> None:
    token = uuid4().hex
    year = 2100 + int(token[:4], 16) % 7000
    edition = Edition(
        country=f"Routing {token[:8]}",
        country_code="FR",
        period_start=date(year, 1, 1),
        period_end=date(year, 1, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Routing persistence test",
        slug=f"routing-{token}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.commit()

    expected_version = 1
    history = []
    for version, (indicators, facts, decisions, expected_kind) in enumerate(
        (
            ((), ("initial facts",), {}, PublicationKind.BRIEF),
            (
                (_indicator("c2.example", ArtifactType.DOMAIN),),
                ("confirmed subject IOC",),
                {
                    "c2.example": (
                        RelevanceClassification.DIRECT,
                        RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                    )
                },
                PublicationKind.ARTICLE,
            ),
            ((), ("requalified facts",), {}, PublicationKind.BRIEF),
        ),
        start=1,
    ):
        if version > 1:
            async with uow_factory() as uow:
                await uow.production_artifacts.mark_from_stage_stale(run.id, "extraction")
                await uow.commit()

        extraction = _extraction(
            indicators=indicators,
            facts=facts,
            subject_id=subject.id,
        )
        projection = _projection(extraction, decisions=decisions)
        extraction_artifact = _artifact(run, ProductionArtifactStage.EXTRACTION, version)
        relevance_artifact = _artifact(run, ProductionArtifactStage.RELEVANCE_PROJECTION, version)
        async with uow_factory() as uow:
            await uow.production_artifacts.append(extraction_artifact)
            await uow.production_artifacts.append(relevance_artifact)
            await uow.commit()

        decision = _decision(
            run=run,
            extraction=extraction,
            projection=projection,
            extraction_artifact=extraction_artifact,
            relevance_artifact=relevance_artifact,
            now=NOW.replace(second=version),
        )
        assert decision.status is RoutingStatus.DECIDED
        assert decision.publication_kind is expected_kind
        async with uow_factory() as uow:
            stored = await uow.production_routing_decisions.append_and_adopt(
                decision,
                expected_version=expected_version,
            )
            await uow.commit()
        assert stored.decision_id == decision.decision_id
        history.append(decision)
        expected_version += 1

        if version == 1:
            # A replay with the original input key is a no-op even if the caller
            # still holds the version that was current before the first adoption.
            async with uow_factory() as uow:
                replay = await uow.production_routing_decisions.append_and_adopt(
                    decision,
                    expected_version=1,
                )
                current_run = await uow.production_runs.get(run.id)
                assert current_run is not None and current_run.version == 2
                assert current_run.routing_decision_id == decision.decision_id
                await uow.commit()
            assert replay.decision_hash == decision.decision_hash
            assert replay.decided_at == decision.decided_at

            contradictory = replace(decision, decision_hash="f" * 64)
            with pytest.raises(ProductionRoutingDecisionConflictError):
                async with uow_factory() as uow:
                    await uow.production_routing_decisions.append_and_adopt(
                        contradictory,
                        expected_version=2,
                    )

    async with uow_factory() as uow:
        stored_history = await uow.production_routing_decisions.list_for_run(run.id)
        current_run = await uow.production_runs.get(run.id)
    assert [item.publication_kind for item in stored_history] == [
        PublicationKind.BRIEF,
        PublicationKind.ARTICLE,
        PublicationKind.BRIEF,
    ]
    assert len({item.decision_id for item in stored_history}) == 3
    assert len({item.input_hash for item in stored_history}) == 3
    assert current_run is not None
    assert current_run.routing_decision_id == stored_history[-1].decision_id

    assert current_run.pipeline_generation == 0
    current_run.pipeline_generation = 1
    async with uow_factory() as uow:
        await uow.production_runs.save(current_run)
        await uow.commit()
    with pytest.raises(StaleProductionRoutingGenerationError):
        async with uow_factory() as uow:
            await uow.production_routing_decisions.append_and_adopt(
                history[-1],
                expected_version=current_run.version,
            )

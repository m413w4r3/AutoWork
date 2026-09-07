"""LOT 38: a References rebuild reuses every unchanged Q2 source."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest

from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageParameters,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_repairs import ProductionReferenceRepairService
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.collection import CollectionState
from cti_app.domain.production import (
    ProductionArtifactStage,
    SubjectProductionStage,
    SubjectProductionStatus,
)

from .support import ProductionScenario

pytestmark = pytest.mark.integration

URLS = tuple(f"https://example.test/lot38-{index}" for index in range(1, 7))


def _references() -> str:
    lines = ["# REFERENCES", "editorial-title: [Publication] LOT 38", ""]
    for index, url in enumerate(URLS, start=1):
        lines.extend(
            (
                f"## SOURCE S{index}",
                f"title: LOT 38 source {index}",
                f"url: {url}",
                f"publisher: LOT 38 Labs {index}",
                f"published-at: 2026-08-{10 + index:02d}",
                f"role: {'primary' if index == 1 else 'independent'}",
                "",
            )
        )
    lines.extend(
        (
            "## EVENT R1",
            "date: 2026-08-20",
            "sources: " + ", ".join(f"S{index}" for index in range(1, 7)),
            "text: ExampleRAT infrastructure is documented across the corpus.",
        )
    )
    return "\n".join(lines)


def _sources() -> dict[str, dict[str, object]]:
    return {
        url: {
            "status": 200 if index < 6 else 404,
            "mime": "text/plain",
            "body": (
                f"LOT 38 source {index} documents ExampleRAT and source-{index}.security-lab.io."
            ),
        }
        for index, url in enumerate(URLS, start=1)
    }


def _q2(index: int) -> str:
    return (
        "FACT malware\n"
        "- ExampleRAT :: The source documents the ExampleRAT family.\n\n"
        "IOC confirmed domain\n"
        f"- source-{index}.security-lab.io :: Infrastructure observed in source {index}."
    )


async def _retry_extraction(scenario: ProductionScenario) -> None:
    assert scenario.run_id is not None
    retry = await SubjectProductionService(scenario.uow_factory).retry_from_stage(
        scenario.run_id, SubjectProductionStage.EXTRACTION
    )
    parameters = ProductionStageParameters(
        run_id=retry.run.id,
        expected_stage=SubjectProductionStage.EXTRACTION.value,
        pipeline_generation=retry.run.pipeline_generation,
    )
    job = await scenario.jobs.submit(
        kind=stage_job_kind(SubjectProductionStage.EXTRACTION),
        aggregate_type="subject",
        aggregate_id=retry.run.subject_id,
        idempotency_key=production_stage_idempotency_key(
            retry.run, SubjectProductionStage.EXTRACTION
        ),
        correlation_id="lot38-rebuild",
        input_parameters=parameters.model_dump(mode="json"),
        max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
        actor_id="lot38-test",
    )
    await scenario.runner.dispatch(job.id)
    await scenario.runner.run_until_idle()


@pytest.mark.asyncio
async def test_references_rebuild_reuses_five_q2_sources_and_calls_s6_once(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = production_scenario_factory(_sources())
    scenario.restrict_core_sources(URLS[:5])
    scenario.model.script.references(_references())
    for index, url in enumerate(URLS, start=1):
        scenario.model.script.q2(source_url=url, access_mode="live_url", response=_q2(index))
    scenario.model.script.synthesis("ExampleRAT activity is documented by the core report [S1].")

    await scenario.start()
    initial = await scenario.run_until_terminal()
    assert initial.status is SubjectProductionStatus.READY
    initial_q2 = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert len(initial_q2) == 5
    assert {call.source_url for call in initial_q2} == set(URLS[:5])

    collections = await scenario.collection_service.list_sources(scenario.subject.id)
    s6 = next(collection for collection in collections if collection.canonical_url == URLS[5])
    assert s6.state is CollectionState.FAILED_TERMINAL
    await scenario.collection_service.archive_manual_content(
        s6.id,
        content=(b"LOT 38 source 6 documents ExampleRAT and source-6.security-lab.io."),
        declared_mime_type="text/plain",
        final_url=URLS[5],
        actor_id="lot38-analyst",
    )

    assert scenario.run_id is not None
    repaired = await ProductionReferenceRepairService(
        scenario.uow_factory, scenario.artifact_store
    ).rebuild_from_archived_q1(scenario.run_id, actor_id="lot38-analyst")
    assert repaired.changed is True
    assert [item["canonical_url"] for item in repaired.source_delta["added_sources"]] == [URLS[5]]
    assert {item["canonical_url"] for item in repaired.source_delta["unchanged_sources"]} == set(
        URLS[:5]
    )

    call_count_before_retry = len(scenario.model.calls)
    await _retry_extraction(scenario)
    retry = await scenario.run_until_terminal()
    assert retry.status is SubjectProductionStatus.READY

    retry_calls = scenario.model.calls[call_count_before_retry:]
    retry_q2 = [call for call in retry_calls if call.stage == "extraction"]
    retry_q4 = [call for call in retry_calls if call.stage == "synthesis"]
    assert len(retry_q2) == 1
    assert retry_q2[0].source_url == URLS[5]
    assert len(retry_q4) <= 1

    async with scenario.uow_factory() as uow:
        extraction = await uow.production_artifacts.get_current(
            scenario.run_id, ProductionArtifactStage.EXTRACTION.value
        )
    assert extraction is not None
    diagnostics = extraction.metadata["deterministic_verification"]
    assert diagnostics["cache_hits"] == 5
    assert diagnostics["model_calls"] == 1

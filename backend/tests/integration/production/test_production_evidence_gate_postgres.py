"""PostgreSQL coverage for the production subject-evidence review gate."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.production_jobs import ProductionStageChain
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_relevance import (
    relevance_projection_from_json,
    subject_relevance_evidence_counts,
)
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionRunStatus,
    ProductionStage,
)

from .support import ProductionScenario, grounded_editorial_proposal
from .test_pipeline_happy_path import (
    Q1_RESPONSE,
    Q2_SECONDARY_RESPONSE,
    SOURCE_URLS,
    _configured_scenario,
    _install_canonical_synthesis,
    _sources,
)

pytestmark = [pytest.mark.integration, pytest.mark.production_evidence_gate]

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]
EVIDENCE_MINIMUM = 4


def _scenario_with_four_direct_facts(
    factory: ScenarioFactory,
) -> ProductionScenario:
    sources = _sources()
    # Synthetic source text and matching quotes make four independently
    # verified primary facts trivial for this threshold boundary test.
    added_source_text = (
        " The ExampleRAT campaign deployed ExampleRAT malware."
        " The ExampleRAT loader executed during the ExampleRAT campaign."
        " The ExampleRAT campaign used a dedicated command channel."
        " The ExampleRAT campaign began with a targeted deployment sequence."
    )
    original_body = str(sources[SOURCE_URLS[0]]["body"])
    sources[SOURCE_URLS[0]]["body"] = original_body.replace(
        "</body></html>", f"{added_source_text}</body></html>"
    )
    scenario = factory(sources)
    scenario.model.script.references(Q1_RESPONSE)
    scenario.model.script.q2(
        source_url=SOURCE_URLS[0],
        response="""FACT malware
- ExampleRAT campaign :: The ExampleRAT campaign deployed ExampleRAT malware.
- ExampleRAT loader :: The ExampleRAT loader executed during the ExampleRAT campaign.
- ExampleRAT command channel :: The ExampleRAT campaign used a dedicated command channel.
- ExampleRAT deployment :: The ExampleRAT campaign began with a targeted deployment sequence.
""",
    )
    scenario.model.script.q2(source_url=SOURCE_URLS[1], response=Q2_SECONDARY_RESPONSE)
    return scenario


async def _run_state(scenario: ProductionScenario) -> Any:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        run = await uow.production_runs.get(scenario.run_id)
    assert run is not None
    return run


async def test_below_threshold_override_survives_restart_and_resumes_once(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = await _configured_scenario(production_scenario_factory)
    scenario.model.script.editorial_enrichment(grounded_editorial_proposal)
    _install_canonical_synthesis(scenario)

    await scenario.start()
    review_run = await scenario.run_until_terminal()

    assert review_run.status is ProductionRunStatus.NEEDS_REVIEW
    assert review_run.current_stage is ProductionStage.SYNTHESIS
    assert review_run.error_code == "production_insufficient_subject_evidence"
    assert review_run.error_details is not None
    assert review_run.error_message == (
        "Le sujet n'est étayé que par "
        f"{review_run.error_details['direct_count']} éléments directs ; "
        "la synthèse serait générique."
    )
    assert review_run.error_details["minimum"] == EVIDENCE_MINIMUM
    assert review_run.error_details["direct_count"] < EVIDENCE_MINIMUM
    assert isinstance(review_run.error_details["context_count"], int)
    assert isinstance(review_run.error_details["out_of_scope_count"], int)
    assert UUID(str(review_run.error_details["projection_artifact_id"]))
    assert not [call for call in scenario.model.calls if call.stage == "synthesis"]

    assert scenario.run_id is not None
    service = SubjectProductionService(scenario.uow_factory)
    override = await service.retry_from_stage(
        scenario.run_id,
        ProductionStage.SYNTHESIS,
        override_insufficient_subject_evidence=True,
        override_actor_id="postgres-gate-reviewer",
        override_reason="Proceed after reviewing the source coverage.",
    )
    assert override.review_override is not None
    assert override.review_override["actor_id"] == "postgres-gate-reviewer"
    assert override.review_override["minimum"] == EVIDENCE_MINIMUM

    # Load the run through a fresh runtime and UoW to prove the JSONB decision
    # survives a process-style restart before synthesis is dispatched.
    restarted = await scenario.restart(scenario.uow_factory)
    persisted = await _run_state(restarted)
    assert len(persisted.review_overrides) == 1
    persisted_decision = next(iter(persisted.review_overrides.values()))
    assert persisted_decision == override.review_override
    generation_after_override = persisted.pipeline_generation
    version_after_override = persisted.version

    repeated = await SubjectProductionService(restarted.uow_factory).retry_from_stage(
        persisted.id,
        ProductionStage.SYNTHESIS,
        override_insufficient_subject_evidence=True,
        override_actor_id="postgres-gate-reviewer",
        override_reason="A repeated request must preserve the first decision.",
    )
    assert repeated.already_applied
    assert repeated.review_override == override.review_override
    assert repeated.run.pipeline_generation == generation_after_override
    assert repeated.run.version == version_after_override
    assert restarted.runner.enqueued == []

    restarted.model.script.editorial_enrichment(grounded_editorial_proposal)
    synthesis_requests = _install_canonical_synthesis(restarted)
    chain = ProductionStageChain(ProductionPacingPolicy.zero())
    chain.bind(restarted.jobs, restarted.runner)
    job_id = await chain.submit(
        run=persisted,
        stage=ProductionStage.SYNTHESIS,
        correlation_id="production-evidence-gate-integration",
        actor_id="postgres-gate-reviewer",
    )
    assert job_id is not None
    await restarted.runner.run_until_idle()

    completed = await _run_state(restarted)
    assert completed.status is ProductionRunStatus.READY, (
        completed.error_code,
        completed.error_message,
        completed.error_details,
    )
    assert completed.current_stage is ProductionStage.ASSEMBLY
    assert len(synthesis_requests) == 1
    assert len([call for call in restarted.model.calls if call.stage == "synthesis"]) == 1


@pytest.mark.asyncio
async def test_at_threshold_proceeds_through_synthesis(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _scenario_with_four_direct_facts(production_scenario_factory)
    scenario.model.script.editorial_enrichment(grounded_editorial_proposal)
    synthesis_requests = _install_canonical_synthesis(scenario)

    await scenario.start()
    completed = await scenario.run_until_terminal()

    assert completed.status is ProductionRunStatus.READY, (
        completed.error_code,
        completed.error_message,
        completed.error_details,
    )
    assert completed.current_stage is ProductionStage.ASSEMBLY
    assert len(synthesis_requests) == 1

    async with scenario.uow_factory() as uow:
        projection_artifact = await uow.production_artifacts.get_current(
            completed.id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
        )
    assert projection_artifact is not None
    assert projection_artifact.canonical_blob_id is not None
    projection = relevance_projection_from_json(
        await scenario.artifact_store.read_json(projection_artifact.canonical_blob_id)
    )
    counts = subject_relevance_evidence_counts(projection)
    assert counts["direct_count"] >= EVIDENCE_MINIMUM

"""End-to-end review scenarios: why a production stops and what stays untouched.

Each test drives the real pipeline on PostgreSQL with deterministic HTTP and a
scripted model, then asserts artifacts, run state, the persisted routing
decision, diagnostics events and the external calls actually made.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from cti_app.application.production_jobs import stage_job_kind
from cti_app.domain.jobs import JobStatus
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_routing import RoutingReasonCode, RoutingStatus

from .support import ProductionScenario
from .test_source_collection_failures import _diagnostic_events

pytestmark = pytest.mark.integration

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]

S1 = "https://example.test/review-1"
S2 = "https://example.test/review-2"
_SYNTHESIS_TEMPLATES = ("production-synthesis", "production-brief-synthesis")


def _specs(
    statuses: Mapping[str, int], *, with_indicator: bool = False
) -> dict[str, dict[str, object]]:
    """Source bodies; an IOC is only verifiable when its value appears in the text."""

    def body(index: int, status: int) -> str:
        if status != 200:
            return "denied"
        if with_indicator:
            return f"ExampleRAT source {index} source-{index}.security-lab.io was archived."
        return f"ExampleRAT source {index} was documented without any indicator."

    return {
        url: {"status": status, "mime": "text/plain", "body": body(index, status)}
        for index, (url, status) in enumerate(statuses.items(), start=1)
    }


def _references(urls: Sequence[str], *, published: bool = True) -> str:
    lines = ["# REFERENCES", "editorial-title: [Publication] Review scenarios", ""]
    for index, url in enumerate(urls, start=1):
        lines.extend(
            (
                f"## SOURCE S{index}",
                f"title: ExampleRAT review source {index}",
                f"url: {url}",
                f"publisher: Review Lab {index}",
            )
        )
        if published:
            lines.append(f"published-at: 2026-08-{10 + index:02d}")
        lines.extend(
            (
                f"role: {'primary' if index == 1 else 'independent'}",
                "reason: Coverage of the ExampleRAT activity",
                "",
            )
        )
    return "\n".join(lines)


def _q2_without_indicator() -> str:
    return "FACT malware\n- ExampleRAT :: The source documents the ExampleRAT family."


def _q2_with_indicator(index: int) -> str:
    return (
        f"{_q2_without_indicator()}\n\n"
        "IOC confirmed domain\n"
        f"- source-{index}.security-lab.io :: Infrastructure observed in source {index}."
    )


def _synthesis_calls(scenario: ProductionScenario) -> list[Any]:
    return [
        call
        for call in scenario.model.calls
        if call.request.prompt_template_id in _SYNTHESIS_TEMPLATES
    ]


async def _routing(scenario: ProductionScenario) -> tuple[Any, Any, list[Any]]:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        run = await uow.production_runs.get(scenario.run_id)
        assert run is not None
        decision = (
            await uow.production_routing_decisions.get(run.routing_decision_id)
            if run.routing_decision_id is not None
            else None
        )
        artifacts = list(await uow.production_artifacts.list_for_run(scenario.run_id))
    return run, decision, artifacts


def _stages(artifacts: Sequence[Any]) -> set[ProductionArtifactStage]:
    return {artifact.stage for artifact in artifacts}


@pytest.mark.asyncio
async def test_inaccessible_core_source_without_confirmed_ioc_stops_at_routing(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """A CORE publication refused by its site blocks the route; nothing is published."""
    scenario = production_scenario_factory(_specs({S1: 200, S2: 403}))
    scenario.model.script.references(_references((S1,)))
    scenario.model.script.q2(source_url=S1, response=_q2_without_indicator())

    await scenario.start()
    await scenario.run_until_terminal()
    run, decision, artifacts = await _routing(scenario)

    assert run.status is ProductionRunStatus.NEEDS_REVIEW
    assert run.error_code == "publication_routing_needs_review"
    assert run.current_stage is ProductionStage.RELEVANCE_PROJECTION
    assert decision is not None
    assert decision.status is RoutingStatus.NEEDS_REVIEW
    assert decision.reason_code is RoutingReasonCode.CRITICAL_SOURCE_INACCESSIBLE
    assert decision.publication_kind is None
    blocked = [item for item in decision.critical_source_diagnostics if item.source_url == S2]
    assert blocked, decision.critical_source_diagnostics
    assert blocked[0].tier == "core"
    assert blocked[0].omission_collection_state is not None
    assert blocked[0].omission_collection_state.value == "blocked"
    assert blocked[0].human_waiver_path.value == "not_consumed_by_routing"

    # No downstream work and nothing published.
    assert _synthesis_calls(scenario) == []
    assert _stages(artifacts).isdisjoint(
        {
            ProductionArtifactStage.SYNTHESIS,
            ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            ProductionArtifactStage.PUBLICATION,
        }
    )
    # The refused source was requested a bounded number of times, never rewritten.
    assert {request.url for request in scenario.collection_transport.requests} <= {S1, S2}

    # Replaying the review adds neither a second decision nor a model call.
    calls_before = len(scenario.model.calls)
    events_before = len(_diagnostic_events(scenario))
    await scenario.enqueue_persisted_jobs()
    await scenario.run_until_terminal()
    run_after, decision_after, _ = await _routing(scenario)
    assert decision_after is not None
    assert decision_after.decision_hash == decision.decision_hash
    assert run_after.status is ProductionRunStatus.NEEDS_REVIEW
    assert len(scenario.model.calls) == calls_before
    assert len(_diagnostic_events(scenario)) == events_before


@pytest.mark.asyncio
async def test_confirmed_ioc_keeps_the_article_route_despite_an_inaccessible_core_source(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """Control: the same refusal does not block when an IOC is confirmed elsewhere."""
    scenario = production_scenario_factory(_specs({S1: 200, S2: 403}, with_indicator=True))
    scenario.model.script.references(_references((S1,)))
    scenario.model.script.q2(source_url=S1, response=_q2_with_indicator(1))

    await scenario.start()
    run = await scenario.run_until_terminal()
    _, decision, artifacts = await _routing(scenario)

    assert run.status is ProductionRunStatus.READY
    assert decision is not None
    assert decision.reason_code is RoutingReasonCode.SUBJECT_IOC_CONFIRMED
    assert ProductionArtifactStage.PUBLICATION in _stages(artifacts)


@pytest.mark.asyncio
async def test_brief_without_a_verified_date_stops_at_routing_before_any_synthesis_call(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """No confirmed IOC and no source-dated entry: the brief route is not announced."""
    scenario = production_scenario_factory(_specs({S1: 200}))
    scenario.model.script.references(_references((S1,), published=False))
    scenario.model.script.q2(source_url=S1, response=_q2_without_indicator())
    # The harness seeds a publication date on every candidate; remove it so that
    # neither the source nor the frozen snapshot offers an eligible date.
    for candidate in scenario.source_candidates:
        candidate.published_at = None

    await scenario.start()
    await scenario.run_until_terminal()
    run, decision, artifacts = await _routing(scenario)

    assert run.status is ProductionRunStatus.NEEDS_REVIEW
    assert run.error_code == "publication_routing_needs_review"
    assert decision is not None
    assert decision.reason_code is RoutingReasonCode.BRIEF_EVIDENCE_CHRONOLOGY_MISSING
    assert decision.publication_kind is None
    assert [item.failure_code for item in decision.critical_source_diagnostics] == [
        "brief_chronology_source_undated"
    ]
    assert _synthesis_calls(scenario) == []
    assert ProductionArtifactStage.SYNTHESIS not in _stages(artifacts)
    assert ProductionArtifactStage.PUBLICATION not in _stages(artifacts)


@pytest.mark.asyncio
async def test_brief_with_an_eligible_publication_date_is_decided_and_synthesised(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """Control: the same subject with a source publication date takes the brief route."""
    scenario = production_scenario_factory(_specs({S1: 200}))
    scenario.model.script.references(_references((S1,), published=True))
    scenario.model.script.q2(source_url=S1, response=_q2_without_indicator())

    await scenario.start()
    run = await scenario.run_until_terminal()
    _, decision, artifacts = await _routing(scenario)

    assert decision is not None
    assert decision.reason_code is RoutingReasonCode.NO_SUBJECT_IOC
    assert decision.publication_kind is not None
    assert decision.publication_kind.value == "brief"
    assert run.status is ProductionRunStatus.READY
    assert _synthesis_calls(scenario)
    assert ProductionArtifactStage.PUBLICATION in _stages(artifacts)


async def _fingerprint(scenario: ProductionScenario) -> dict[str, Any]:
    """Everything a replay must leave byte-for-byte identical."""
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        run = await uow.production_runs.get(scenario.run_id)
        assert run is not None
        artifacts = list(await uow.production_artifacts.list_for_run(scenario.run_id))
        decision = (
            await uow.production_routing_decisions.get(run.routing_decision_id)
            if run.routing_decision_id is not None
            else None
        )
        provenance = list(await uow.provenance.list_for_aggregate("subject", scenario.subject.id))
        collections = list(await uow.source_collections.list_for_subject(scenario.subject.id))
        provenance += [
            event
            for collection in collections
            for event in await uow.provenance.list_for_aggregate("source_collection", collection.id)
        ]
    return {
        "status": run.status,
        "generation": run.pipeline_generation,
        "artifacts": sorted(
            (
                artifact.stage.value,
                artifact.version,
                artifact.status.value,
                artifact.input_hash,
                str(artifact.canonical_blob_id),
            )
            for artifact in artifacts
        ),
        "decision": (decision.decision_id, decision.decision_hash) if decision else None,
        "provenance": sorted(str(event.id) for event in provenance),
        "diagnostic_events": len(_diagnostic_events(scenario)),
        "model_calls": len(scenario.model.calls),
        "transport_requests": len(scenario.collection_transport.requests),
    }


@pytest.mark.asyncio
async def test_finished_extraction_and_assembly_job_replay_is_safe(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """Lost acknowledgements replay handlers without changing durable production state."""
    scenario = production_scenario_factory(_specs({S1: 200}, with_indicator=True))
    scenario.model.script.references(_references((S1,)))
    scenario.model.script.q2(source_url=S1, response=_q2_with_indicator(1))

    await scenario.start()
    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.READY
    before = await _fingerprint(scenario)
    # The comparison is only meaningful if the fingerprint actually captures history.
    assert len(before["artifacts"]) >= len(ProductionArtifactStage)
    assert before["provenance"]
    assert before["diagnostic_events"] > 0
    assert before["model_calls"] > 0
    assert before["decision"] is not None

    invariant_keys = (
        "status",
        "generation",
        "artifacts",
        "decision",
        "provenance",
        "model_calls",
        "transport_requests",
    )
    expected_invariants = {key: before[key] for key in invariant_keys}

    for stage in (ProductionStage.EXTRACTION, ProductionStage.ASSEMBLY):
        job = next(
            job
            for job in await scenario.jobs.list_for_aggregate("subject", scenario.subject.id)
            if job.kind == stage_job_kind(stage)
        )
        assert job.status is JobStatus.SUCCEEDED
        previous_attempt = job.attempt
        previous_history = await scenario.jobs.history(job.id)
        diagnostic_count = len(_diagnostic_events(scenario))

        async with scenario.uow_factory() as uow:
            queued = await uow.jobs.get_for_update(job.id)
            assert queued is not None
            assert queued.status is JobStatus.SUCCEEDED
            queued.status = JobStatus.QUEUED
            queued.finished_at = None
            queued.next_retry_at = None
            await uow.jobs.save(queued)
            await uow.commit()

        await scenario.runner.dispatch(job.id)
        await scenario.runner.run_until_idle()

        replayed = await scenario.jobs.get(job.id)
        history = await scenario.jobs.history(job.id)
        assert replayed.status is JobStatus.SUCCEEDED
        assert replayed.attempt == previous_attempt + 1
        new_events = history[len(previous_history) :]
        assert any(event.event_type == "job.started" for event in new_events)
        assert any(event.event_type == "job.succeeded" for event in new_events)

        after = await _fingerprint(scenario)
        assert {key: after[key] for key in invariant_keys} == expected_invariants
        if stage is ProductionStage.ASSEMBLY:
            diagnostics = _diagnostic_events(scenario)[diagnostic_count:]
            assert any(
                event.get("event") == "stage.outcome"
                and event.get("stage") == ProductionStage.ASSEMBLY.value
                for event in diagnostics
            )

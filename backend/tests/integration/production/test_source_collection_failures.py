"""Business contracts when a publication cannot be downloaded at all.

The other business suites always serve HTTP 200, so the collection boundary is
only ever exercised on its happy path.  These tests drive the real
SafeHttpCollector into its terminal and retryable failures and pin what the
pipeline is allowed to do afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from cti_app.domain.collection import CollectionState
from cti_app.domain.production import (
    ProductionArtifactStage,
    SubjectProductionStage,
    SubjectProductionStatus,
)

from .support import ProductionScenario

pytestmark = pytest.mark.integration

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]

S1 = "https://example.test/collect-1"
S2 = "https://example.test/collect-2"


def _body(index: int) -> str:
    return f"ExampleRAT source {index} source-{index}.security-lab.io was archived."


def _specs(statuses: Mapping[str, int]) -> dict[str, dict[str, object]]:
    return {
        url: {
            "status": statuses[url],
            "mime": "text/plain",
            "body": _body(index) if statuses[url] == 200 else "not found",
        }
        for index, url in enumerate(statuses, start=1)
    }


def _references(urls: Sequence[str]) -> str:
    lines = ["# REFERENCES", "editorial-title: [Publication] Collection failures", ""]
    for index, url in enumerate(urls, start=1):
        lines.extend(
            (
                f"## SOURCE S{index}",
                f"title: ExampleRAT collection source {index}",
                f"url: {url}",
                f"publisher: Collect Lab {index}",
                f"published-at: 2026-08-{10 + index:02d}",
                f"role: {'primary' if index == 1 else 'independent'}",
                "",
            )
        )
    lines.extend(
        (
            "## EVENT R1",
            "date: 2026-08-15",
            "sources: " + ", ".join(f"S{index}" for index in range(1, len(urls) + 1)),
            "text: ExampleRAT activity is documented by these publications.",
        )
    )
    return "\n".join(lines)


def _q2(index: int) -> str:
    return (
        "FACT malware\n"
        "- ExampleRAT :: The source documents the ExampleRAT family.\n\n"
        "IOC confirmed domain\n"
        f"- source-{index}.security-lab.io :: Infrastructure observed in source {index}."
    )


def _diagnostic_events(scenario: ProductionScenario) -> list[dict[str, Any]]:
    path = scenario.blob_root.parent / "diagnostics" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _state(scenario: ProductionScenario) -> tuple[Any, list[Any], list[Any]]:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        run = await uow.subject_production_runs.get(scenario.run_id)
        artifacts = list(await uow.production_artifacts.list_for_run(scenario.run_id))
        collections = list(await uow.source_collections.list_for_subject(scenario.subject.id))
    assert run is not None
    return run, artifacts, collections


@pytest.mark.asyncio
async def test_every_core_source_unreachable_stops_before_any_model_call(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """Nothing may be sent to the model when no publication could be archived."""
    scenario = production_scenario_factory(_specs({S1: 404}))

    await scenario.start()
    run = await scenario.run_until_terminal()
    persisted, artifacts, collections = await _state(scenario)

    assert run.status is not SubjectProductionStatus.READY
    assert persisted.current_stage is SubjectProductionStage.SOURCES
    assert scenario.model.calls == []
    assert scenario.model.provider_calls == []
    assert artifacts == []
    assert [request.url for request in scenario.collection_transport.requests] == [S1]
    assert collections
    assert all(collection.state is not CollectionState.ARCHIVED for collection in collections)


@pytest.mark.asyncio
async def test_one_unreachable_core_source_does_not_stop_the_other(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """A single dead publication must not cost the run its surviving source."""
    scenario = production_scenario_factory(_specs({S1: 200, S2: 404}))
    scenario.model.script.references(_references((S1,)))
    scenario.model.script.q2(source_url=S1, access_mode="live_url", response=_q2(1))
    scenario.model.script.synthesis("ExampleRAT activity is documented [S1].")

    await scenario.start()
    run = await scenario.run_until_terminal()
    persisted, artifacts, collections = await _state(scenario)

    assert run.status is SubjectProductionStatus.READY
    assert persisted.current_stage is SubjectProductionStage.ASSEMBLY
    archived = {
        collection.canonical_url
        for collection in collections
        if collection.state is CollectionState.ARCHIVED
    }
    assert archived == {S1}
    assert {artifact.stage for artifact in artifacts} == set(ProductionArtifactStage)
    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert [call.source_url for call in q2_calls] == [S1]
    assert all(S2 not in call.source_urls for call in q2_calls)


@pytest.mark.asyncio
async def test_unreachable_q1_source_is_a_warning_and_never_reaches_q2(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """A supplemental publication Q1 proposed but that 404s is dropped, not fatal."""
    scenario = production_scenario_factory(_specs({S1: 200, S2: 404}))
    scenario.restrict_core_sources((S1,))
    # Q1 proposes both: S2 is only reachable through supplemental collection.
    scenario.model.script.references(_references((S1, S2)))
    scenario.model.script.q2(source_url=S1, access_mode="live_url", response=_q2(1))
    scenario.model.script.synthesis("ExampleRAT activity is documented [S1].")

    await scenario.start()
    run = await scenario.run_until_terminal()
    persisted, artifacts, collections = await _state(scenario)

    assert run.status is SubjectProductionStatus.READY
    assert persisted.current_stage is SubjectProductionStage.ASSEMBLY
    assert {
        collection.canonical_url
        for collection in collections
        if collection.state is CollectionState.ARCHIVED
    } == {S1}

    references = next(
        artifact for artifact in artifacts if artifact.stage is ProductionArtifactStage.REFERENCES
    )
    assert any(
        warning.startswith("supplemental_collection_failed")
        for warning in references.metadata["warnings"]
    ), references.metadata["warnings"]
    assert any(
        event.get("event") == "q1.supplemental_collection_failed"
        for event in _diagnostic_events(scenario)
    )

    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert [url for call in q2_calls for url in call.source_urls] == [S1]
    assert persisted.extraction_progress is not None
    assert {item["source_id"] for item in persisted.extraction_progress["sources"]} == {"S1"}


@pytest.mark.asyncio
async def test_retryable_collection_failure_is_attempted_once_and_left_recoverable(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """A 503 supplemental source is attempted once, then left in a retryable state.

    The targeted `prepare_retry` path only applies to a collection that already
    entered the stage as FAILED_RETRYABLE; a source failing for the first time
    inside References must not be re-downloaded in the same stage.
    """
    scenario = production_scenario_factory(_specs({S1: 200, S2: 503}))
    scenario.restrict_core_sources((S1,))
    scenario.model.script.references(_references((S1, S2)))
    scenario.model.script.q2(source_url=S1, access_mode="live_url", response=_q2(1))
    scenario.model.script.synthesis("ExampleRAT activity is documented [S1].")

    await scenario.start()
    run = await scenario.run_until_terminal()
    persisted, _, collections = await _state(scenario)

    assert run.status is SubjectProductionStatus.READY
    failed = next(collection for collection in collections if collection.canonical_url == S2)
    assert failed.state is CollectionState.FAILED_RETRYABLE
    s2_requests = [
        request for request in scenario.collection_transport.requests if request.url == S2
    ]
    assert len(s2_requests) == 1
    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert [url for call in q2_calls for url in call.source_urls] == [S1]
    assert persisted.extraction_progress is not None
    assert {item["source_id"] for item in persisted.extraction_progress["sources"]} == {"S1"}

"""LOT 38: a canonical References rebuild reuses every unchanged extraction."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelCapabilities,
    ModelRole,
    ModelSubmissionReconciliationRequiredError,
    ModelUsage,
    SafeModelRequest,
)
from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageParameters,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2FactProposal,
    Q2SourceOutput,
)
from cti_app.application.production_q2_batch import Q2BatchResponse, Q2BatchSourceOutput
from cti_app.application.production_repairs import ProductionReferenceRepairService
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.collection import CollectionState
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_extraction import (
    ExtractionReuseState,
    ProductionExtractionV1,
    production_extraction_from_json,
)

from .support import ProductionScenario

pytestmark = pytest.mark.integration

URLS = tuple(f"https://example.test/lot38-{index}" for index in range(1, 7))

_CANONICAL_SOURCE_TEMPLATE = "production-extraction-archive-source"
_CANONICAL_BATCH_TEMPLATE = "production-extraction-archive-batch"
_BATCH_MARKER = re.compile(r"@@Q2:(B\d+)@@")


def _marker(index: int) -> str:
    return f"source-{index}.security-lab.io"


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
                "kind: publication",
                "reason: Coverage of the ExampleRAT activity",
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


def _manual_six_body() -> str:
    return (
        "LOT 38 source 6 documents ExampleRAT and source-6.security-lab.io, "
        "published with its detection material."
    )


def _sources() -> dict[str, dict[str, object]]:
    return {
        url: {
            "status": 200 if index < 6 else 404,
            "mime": "text/plain",
            "body": (
                f"LOT 38 source {index} documents ExampleRAT and {_marker(index)}, "
                "published for coverage."
            ),
        }
        for index, url in enumerate(URLS, start=1)
    }


def _output(index: int, *, full: bool) -> Q2SourceOutput:
    return Q2SourceOutput(
        facts=[Q2FactProposal(category="malware", value="ExampleRAT")] if full else [],
        artifacts=[
            Q2ArtifactProposal(
                value=_marker(index),
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            )
        ],
        uncertainties=[f"LOT 38 source {index} remains unconfirmed."],
    )


def _batch_blocks(prompt: str) -> tuple[tuple[str, str], ...]:
    matches = list(_BATCH_MARKER.finditer(prompt))
    blocks: list[tuple[str, str]] = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(prompt)
        blocks.append((match.group(1), prompt[match.end() : end]))
    return tuple(blocks)


def _adapter_result(adapter: Any, output: Q2SourceOutput | Q2BatchResponse) -> AdapterResult:
    return AdapterResult(
        status=AdapterResultStatus.COMPLETED,
        provider=adapter.provider,
        requested_model=str(adapter.requested_model),
        actual_model_version=str(adapter.requested_model),
        usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        response_id=f"lot38-{uuid4()}",
        structured_output=output,
    )


@dataclass
class CanonicalExtractionScript:
    """Answer the archive-backed extraction capability of the fake gateway."""

    outputs: Mapping[str, Q2SourceOutput]
    calls: list[SafeModelRequest] = field(default_factory=list)
    ambiguity: ModelSubmissionReconciliationRequiredError | None = None

    def output_for(self, text: str) -> Q2SourceOutput:
        for marker, output in self.outputs.items():
            if marker in text:
                return output
        raise AssertionError("No scripted canonical extraction output for this capture")

    def install(self, scenario: ProductionScenario) -> None:
        adapter = scenario.model._adapter
        adapter.capabilities = ModelCapabilities(
            web_search=True, background=True, conversation=True, structured_output=True
        )
        base_invoke = adapter.invoke

        async def invoke(
            request: SafeModelRequest,
            *,
            role: ModelRole,
            output_schema: type[Any] | None = None,
        ) -> AdapterResult:
            template = request.prompt_template_id
            if template not in {_CANONICAL_SOURCE_TEMPLATE, _CANONICAL_BATCH_TEMPLATE}:
                return await base_invoke(request, role=role, output_schema=output_schema)
            scenario.model.provider_calls.append(request)
            self.calls.append(request)
            if self.ambiguity is not None:
                raise self.ambiguity
            if template == _CANONICAL_BATCH_TEMPLATE:
                response = Q2BatchResponse(
                    sources=[
                        Q2BatchSourceOutput(batch_id=handle, output=self.output_for(body))
                        for handle, body in _batch_blocks(request.text)
                    ]
                )
                return _adapter_result(adapter, response)
            return _adapter_result(adapter, self.output_for(request.text))

        adapter.invoke = invoke


async def _current_extraction(
    scenario: ProductionScenario,
) -> dict[str, Any]:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        artifact = await uow.production_artifacts.get_current(
            scenario.run_id, ProductionArtifactStage.EXTRACTION.value
        )
    assert artifact is not None and artifact.canonical_blob_id is not None
    payload = await scenario.artifact_store.read_json(artifact.canonical_blob_id)
    return payload


async def _retry_extraction(scenario: ProductionScenario) -> None:
    assert scenario.run_id is not None
    retry = await SubjectProductionService(scenario.uow_factory).retry_from_stage(
        scenario.run_id, ProductionStage.EXTRACTION
    )
    parameters = ProductionStageParameters(
        run_id=retry.run.id,
        expected_stage=ProductionStage.EXTRACTION.value,
        pipeline_generation=retry.run.pipeline_generation,
    )
    job = await scenario.jobs.submit(
        kind=stage_job_kind(ProductionStage.EXTRACTION),
        aggregate_type="subject",
        aggregate_id=retry.run.subject_id,
        idempotency_key=production_stage_idempotency_key(retry.run, ProductionStage.EXTRACTION),
        correlation_id="lot38-rebuild",
        input_parameters=parameters.model_dump(mode="json"),
        max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
        actor_id="lot38-test",
    )
    await scenario.runner.dispatch(job.id)
    await scenario.runner.run_until_idle()


@pytest.mark.asyncio
async def test_references_rebuild_reuses_five_core_extractions_and_calls_six_once(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = production_scenario_factory(_sources())
    scenario.restrict_core_sources(URLS[:5])
    scenario.model.script.references(_references())
    scenario.model.script.synthesis("ExampleRAT activity is documented by the core report [S1].")
    script = CanonicalExtractionScript(
        outputs={_marker(index): _output(index, full=index < 6) for index in range(1, 7)}
    )
    script.install(scenario)

    await scenario.start()
    initial = await scenario.run_until_terminal()
    assert initial.status is ProductionRunStatus.READY
    assert len(script.calls) == 5
    assert {call.metadata["profile"] for call in script.calls} == {"full"}

    async with scenario.uow_factory() as uow:
        references = await uow.production_artifacts.get_current(
            initial.id, ProductionArtifactStage.REFERENCES.value
        )
    assert references is not None and references.canonical_blob_id is not None
    corpus = await scenario.artifact_store.read_json(references.canonical_blob_id)
    s6_record = next(source for source in corpus["sources"] if source["canonical_url"] == URLS[5])
    assert s6_record["collection_state"] == CollectionState.UNAVAILABLE.value
    assert s6_record["eligible_for_extraction"] is False

    initial_extraction = _extraction_from(await _current_extraction(scenario))
    assert [source.reuse_state for source in initial_extraction.sources] == [
        ExtractionReuseState.FRESH
    ] * 5

    collections = await scenario.collection_service.list_sources(scenario.subject.id)
    s6 = next(collection for collection in collections if collection.canonical_url == URLS[5])
    assert s6.state is CollectionState.UNAVAILABLE
    await scenario.collection_service.archive_manual_content(
        s6.id,
        content=_manual_six_body().encode("utf-8"),
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

    calls_before = len(script.calls)
    await _retry_extraction(scenario)
    retry = await scenario.run_until_terminal()
    assert retry.status is ProductionRunStatus.READY

    new_calls = script.calls[calls_before:]
    assert len(new_calls) == 1
    assert new_calls[0].metadata["profile"] == "ioc_rules"
    assert _marker(6) in new_calls[0].text

    async with scenario.uow_factory() as uow:
        extraction_artifact = await uow.production_artifacts.get_current(
            scenario.run_id, ProductionArtifactStage.EXTRACTION.value
        )
    assert extraction_artifact is not None
    assert extraction_artifact.metadata["source_count"] == 6
    assert extraction_artifact.metadata["reused_source_count"] == 5
    assert extraction_artifact.metadata["fresh_source_count"] == 1
    retry_extraction = _extraction_from(await _current_extraction(scenario))
    by_url = {source.canonical_url: source for source in retry_extraction.sources}
    assert by_url[URLS[5]].reuse_state is ExtractionReuseState.FRESH
    assert all(by_url[url].reuse_state is ExtractionReuseState.REUSED for url in URLS[:5])


def _extraction_from(payload: dict[str, Any]) -> ProductionExtractionV1:
    return production_extraction_from_json(payload)

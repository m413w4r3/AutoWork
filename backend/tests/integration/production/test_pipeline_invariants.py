"""Canonical EXTRACTION invariants for the complete Production pipeline.

The scenario fixture keeps PostgreSQL, the blob catalog, the real workflow,
jobs and repositories in the loop; only the HTTP and model boundaries are
scripted. EXTRACTION replays one source-local Q2 wire response per archived
capture and never talks to a provider.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID, uuid4

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
from cti_app.application.production_extraction import EXTRACTION_PROFILE_POLICY_VERSION
from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageParameters,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2EventProposal,
    Q2FactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
)
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.collection import CollectionState
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_extraction import (
    ExtractionReuseState,
    ProductionExtractionOmissionReason,
    ProductionExtractionV1,
    production_extraction_from_json,
)
from cti_app.domain.production_references import ProductionReferenceTier

from .support import ProductionScenario, q2_output_to_wire_text

pytestmark = pytest.mark.integration

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]

_CANONICAL_SOURCE_TEMPLATE = "production-extraction-archive-source"
_CANONICAL_BATCH_TEMPLATE = "production-extraction-archive-batch"
_BATCH_MARKER = re.compile(r"@@Q2:(B\d+)@@")
_EVENT_DATE = date(2026, 8, 15)
_RULE_BODY = "rule Support { condition: true }"


def _urls(count: int, *, namespace: str) -> tuple[str, ...]:
    return tuple(f"https://invariants.test/{namespace}-{index}" for index in range(1, count + 1))


def _marker(index: int) -> str:
    return f"source-{index}.security-lab.io"


def _source_body(index: int, *, salt: str = "") -> str:
    return (
        f"{salt}ExampleRAT source {index} was archived on 2026-08-15\n"
        f"The implant beaconed to {_marker(index)}, and the loader persisted.\n"
        f"ExampleRAT is the tracked malware family of source {index}.\n"
        f"{_RULE_BODY}\n"
    )


def _full_output(index: int) -> Q2SourceOutput:
    return Q2SourceOutput(
        facts=[Q2FactProposal(category="malware", value="ExampleRAT")],
        events=[
            Q2EventProposal(
                event_date=_EVENT_DATE,
                text=f"ExampleRAT source {index} was archived on 2026-08-15",
            )
        ],
        artifacts=[
            Q2ArtifactProposal(
                value=_marker(index),
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            )
        ],
        rules=[
            Q2RuleProposal(rule_type=DetectionRuleType.YARA, name="ExampleRAT", body=_RULE_BODY)
        ],
        uncertainties=[f"ExampleRAT source {index} leaves the loader family unconfirmed."],
    )


def _ioc_rules_output(index: int) -> Q2SourceOutput:
    return Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value=_marker(index),
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            )
        ],
        rules=[Q2RuleProposal(rule_type=DetectionRuleType.YARA, name="Support", body=_RULE_BODY)],
        uncertainties=[f"ExampleRAT source {index} leaves the loader family unconfirmed."],
    )


def _references(urls: Sequence[str], *, technical: frozenset[str] = frozenset()) -> str:
    lines = ["# REFERENCES", "editorial-title: [Publication] canonical extraction coverage", ""]
    for index, url in enumerate(urls, start=1):
        lines.extend(
            (
                f"## SOURCE S{index}",
                f"title: Canonical source {index}",
                f"url: {url}",
                f"publisher: Invariant Lab {index}",
                f"published-at: 2026-08-{10 + index:02d}",
                f"role: {'primary' if index == 1 else 'independent'}",
                f"kind: {'technical_resource' if url in technical else 'publication'}",
                "reason: Coverage of the ExampleRAT activity",
                "",
            )
        )
    lines.extend(
        (
            "## EVENT R1",
            "date: 2026-08-15",
            "sources: " + ", ".join(f"S{index}" for index in range(1, len(urls) + 1)),
            "text: The selected reports document the same ExampleRAT activity.",
        )
    )
    return "\n".join(lines)


def _batch_blocks(prompt: str) -> tuple[tuple[str, str], ...]:
    matches = list(_BATCH_MARKER.finditer(prompt))
    blocks: list[tuple[str, str]] = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(prompt)
        blocks.append((match.group(1), prompt[match.end() : end]))
    return tuple(blocks)


def _adapter_result(adapter: Any, output_text: str) -> AdapterResult:
    return AdapterResult(
        status=AdapterResultStatus.COMPLETED,
        provider=adapter.provider,
        requested_model=str(adapter.requested_model),
        actual_model_version=str(adapter.requested_model),
        usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        response_id=f"canonical-{uuid4()}",
        output_text=output_text,
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
                response = "\n\n".join(
                    f"@@Q2:{handle}@@\n{q2_output_to_wire_text(self.output_for(body))}"
                    for handle, body in _batch_blocks(request.text)
                )
                return _adapter_result(adapter, response)
            return _adapter_result(adapter, q2_output_to_wire_text(self.output_for(request.text)))

        adapter.invoke = invoke


def _configure(
    factory: ScenarioFactory,
    urls: tuple[str, ...],
    *,
    core_urls: Sequence[str],
    technical: frozenset[str] = frozenset(),
    unavailable: frozenset[str] = frozenset(),
    body_salt: str = "",
) -> tuple[ProductionScenario, CanonicalExtractionScript]:
    specs = {
        url: {
            "status": 404 if url in unavailable else 200,
            "mime": "text/plain",
            "body": _source_body(index, salt=body_salt),
        }
        for index, url in enumerate(urls, start=1)
    }
    scenario = factory(specs)
    scenario.restrict_core_sources(core_urls)
    scenario.model.script.references(_references(urls, technical=technical))
    core = set(core_urls)
    script = CanonicalExtractionScript(
        outputs={
            _marker(index): _full_output(index) if url in core else _ioc_rules_output(index)
            for index, url in enumerate(urls, start=1)
        }
    )
    script.install(scenario)
    return scenario, script


async def _current_artifact(
    scenario: ProductionScenario, stage: ProductionArtifactStage
) -> ProductionArtifact | None:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        return await uow.production_artifacts.get_current(scenario.run_id, stage.value)


async def _run_to_artifact(
    scenario: ProductionScenario, stage: ProductionArtifactStage, *, attempts: int = 16
) -> ProductionArtifact:
    for _ in range(attempts):
        artifact = await _current_artifact(scenario, stage)
        if artifact is not None:
            return artifact
        assert await scenario.runner.run_next(), f"no job produced the {stage.value} artifact"
    raise AssertionError(f"the {stage.value} artifact never appeared")


async def _canonical_extraction(
    scenario: ProductionScenario, run_id: UUID
) -> ProductionExtractionV1:
    async with scenario.uow_factory() as uow:
        artifact = await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.EXTRACTION.value
        )
    assert artifact is not None and artifact.canonical_blob_id is not None
    payload = await scenario.artifact_store.read_json(artifact.canonical_blob_id)
    return production_extraction_from_json(payload)


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
        correlation_id="canonical-retry",
        input_parameters=parameters.model_dump(mode="json"),
        max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
        actor_id="canonical-retry",
    )
    await scenario.runner.dispatch(job.id)
    await scenario.runner.run_until_idle()


@pytest.mark.asyncio
async def test_extraction_follows_the_frozen_corpus_tier_policy(
    production_scenario_factory: ScenarioFactory,
) -> None:
    urls = _urls(5, namespace="tiers")
    core = (urls[0], urls[1])
    supporting, technical, unavailable = urls[2], urls[3], urls[4]
    scenario, script = _configure(
        production_scenario_factory,
        urls,
        core_urls=core,
        technical=frozenset({technical}),
        unavailable=frozenset({unavailable}),
    )

    await scenario.start()
    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.READY, (run.error_code, run.error_details)

    profiles = sorted(request.metadata["profile"] for request in script.calls)
    assert profiles == ["full", "full", "ioc_rules"]
    batch_calls = [
        call for call in script.calls if call.prompt_template_id == _CANONICAL_BATCH_TEMPLATE
    ]
    assert len(batch_calls) == 1

    artifact = await _current_artifact(scenario, ProductionArtifactStage.EXTRACTION)
    assert artifact is not None and artifact.canonical_blob_id is not None
    assert artifact.status is ProductionArtifactStatus.VERIFIED
    assert artifact.raw_blob_id is None
    assert artifact.model_run_id is None
    metadata = artifact.metadata
    assert metadata["source_count"] == 4
    assert metadata["full_source_count"] == 2
    assert metadata["ioc_rules_source_count"] == 2
    assert metadata["omitted_source_count"] == 1
    assert metadata["fresh_source_count"] == 4
    assert metadata["reused_source_count"] == 0
    assert metadata["profile_policy_version"] == EXTRACTION_PROFILE_POLICY_VERSION
    assert not {"facts", "indicators", "rules", "events", "sources"} & set(metadata)

    extraction = await _canonical_extraction(scenario, run.id)
    assert extraction.profile_policy_version == EXTRACTION_PROFILE_POLICY_VERSION
    assert [source.canonical_url for source in extraction.sources] == [
        core[0],
        core[1],
        supporting,
        technical,
    ]
    by_url = {source.canonical_url: source for source in extraction.sources}
    full = by_url[core[0]]
    assert full.tier is ProductionReferenceTier.CORE
    assert full.profile is ExtractionProfile.FULL
    assert full.reuse_state is ExtractionReuseState.FRESH
    assert [fact.value for fact in full.facts] == ["ExampleRAT"]
    assert [event.event_date for event in full.events] == [_EVENT_DATE]
    assert [indicator.value for indicator in full.indicators] == [_marker(1)]
    assert [rule.body for rule in full.rules] == [_RULE_BODY]
    # ExampleRAT is published by both CORE documents: each source keeps its own
    # entry and the fact names the union of the publishing documents.
    both_core = tuple(
        sorted((full.source_document_id, by_url[core[1]].source_document_id), key=str)
    )
    assert [fact.source_document_ids for fact in full.facts] == [both_core]
    assert [fact.source_document_ids for fact in by_url[core[1]].facts] == [both_core]
    assert [indicator.source_document_ids for indicator in full.indicators] == [
        (full.source_document_id,)
    ]
    light = by_url[supporting]
    assert light.tier is ProductionReferenceTier.SUPPORTING
    assert light.profile is ExtractionProfile.IOC_RULES
    assert light.facts == () and light.events == ()
    assert [indicator.value for indicator in light.indicators] == [_marker(3)]
    assert [rule.body for rule in light.rules] == [_RULE_BODY]
    assert by_url[technical].tier is ProductionReferenceTier.TECHNICAL
    assert by_url[technical].profile is ExtractionProfile.IOC_RULES

    assert [omission.canonical_url for omission in extraction.omitted_sources] == [unavailable]
    omission = extraction.omitted_sources[0]
    assert omission.reason is ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE
    assert omission.collection_state is CollectionState.UNAVAILABLE

    progress = run.extraction_progress
    assert progress is not None
    statuses = {entry["canonical_url"]: entry["status"] for entry in progress["sources"]}
    assert statuses[core[0]] == "succeeded"
    assert statuses[supporting] == "succeeded"
    assert statuses[unavailable] == "omitted"
    assert progress["full_total"] == 2
    assert progress["ioc_rules_total"] == 2
    assert progress["skipped_sources"] == 1


@pytest.mark.asyncio
async def test_archived_bytes_are_verified_before_any_model_call(
    production_scenario_factory: ScenarioFactory,
) -> None:
    urls = _urls(3, namespace="integrity")
    scenario, script = _configure(production_scenario_factory, urls, core_urls=(urls[0], urls[1]))

    await scenario.start()
    references = await _run_to_artifact(scenario, ProductionArtifactStage.REFERENCES)
    assert references.canonical_blob_id is not None
    corpus = await scenario.artifact_store.read_json(references.canonical_blob_id)
    target = next(source for source in corpus["sources"] if source["canonical_url"] == urls[0])

    tampered = await scenario.artifact_store.put_bytes(
        b"tampered archive bytes", bucket="documents", mime_type="text/plain"
    )
    async with scenario.uow_factory() as uow:
        document = await uow.source_documents.get(UUID(target["source_document_id"]))
        assert document is not None
        document.decoded_blob_id = tampered
        await uow.source_documents.save(document)
        await uow.commit()

    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.NEEDS_REVIEW
    assert run.error_code == "extraction_source_content_mismatch"
    assert script.calls == []


@pytest.mark.asyncio
async def test_extraction_runs_without_any_network_access(
    production_scenario_factory: ScenarioFactory,
) -> None:
    urls = _urls(2, namespace="offline")
    scenario, script = _configure(production_scenario_factory, urls, core_urls=urls)

    async def _forbidden(request: object) -> object:
        raise AssertionError(f"EXTRACTION opened the network: {request!r}")

    await scenario.start()
    await _run_to_artifact(scenario, ProductionArtifactStage.REFERENCES)
    scenario.collection_transport.request = _forbidden  # type: ignore[method-assign]

    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.READY, (run.error_code, run.error_details)
    assert len(script.calls) == 2


@pytest.mark.asyncio
async def test_two_providers_produce_the_same_canonical_contract(
    production_scenario_factory: ScenarioFactory,
) -> None:
    first_urls = _urls(2, namespace="provider-one")
    first, first_script = _configure(production_scenario_factory, first_urls, core_urls=first_urls)
    second_urls = _urls(2, namespace="provider-two")
    # Distinct bytes: identical captures would reuse the first provider's
    # content-addressed checkpoints instead of reaching the second provider.
    second, second_script = _configure(
        production_scenario_factory,
        second_urls,
        core_urls=second_urls,
        body_salt="Second provider capture. ",
    )
    second.model.use_chatgpt_bridge_identity()

    await first.start()
    first_run = await first.run_until_terminal()
    await second.start()
    second_run = await second.run_until_terminal()
    assert first_run.status is ProductionRunStatus.READY
    assert second_run.status is ProductionRunStatus.READY
    assert len(first_script.calls) == 2
    assert len(second_script.calls) == 2
    assert first_run.id != second_run.id

    def projection(extraction: ProductionExtractionV1) -> list[tuple[object, ...]]:
        return sorted(
            (
                source.tier.value,
                source.profile.value,
                tuple(fact.value for fact in source.facts),
                tuple(event.event_date for event in source.events),
                tuple(indicator.value for indicator in source.indicators),
                tuple(rule.body for rule in source.rules),
            )
            for source in extraction.sources
        )

    first_extraction = await _canonical_extraction(first, first_run.id)
    second_extraction = await _canonical_extraction(second, second_run.id)
    assert projection(first_extraction) == projection(second_extraction)


@pytest.mark.asyncio
async def test_duplicate_content_keeps_two_sources_and_calls_the_model_once(
    production_scenario_factory: ScenarioFactory,
) -> None:
    urls = _urls(2, namespace="duplicate")
    scenario, script = _configure(production_scenario_factory, urls, core_urls=urls)
    shared_body = _source_body(1)
    shared_spec = {"status": 200, "mime": "text/plain", "body": shared_body}
    scenario.collection_transport._sources = dict.fromkeys(urls, shared_spec)

    await scenario.start()
    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.READY, (run.error_code, run.error_details)
    assert len(script.calls) == 1

    extraction = await _canonical_extraction(scenario, run.id)
    assert [source.canonical_url for source in extraction.sources] == [urls[0], urls[1]]
    digests = {source.content_sha256 for source in extraction.sources}
    assert digests == {extraction.sources[0].content_sha256}
    assert extraction.sources[0].reuse_state is ExtractionReuseState.FRESH
    assert extraction.sources[1].reuse_state is ExtractionReuseState.CONTENT_DUPLICATE
    assert extraction.sources[1].checkpoint_id == extraction.sources[0].checkpoint_id
    assert [indicator.value for indicator in extraction.sources[1].indicators] == [_marker(1)]


@pytest.mark.asyncio
async def test_cross_subject_checkpoints_reuse_the_extraction_without_a_call(
    production_scenario_factory: ScenarioFactory,
) -> None:
    urls = _urls(2, namespace="cross-subject")
    first, first_script = _configure(production_scenario_factory, urls, core_urls=urls)
    await first.start()
    first_run = await first.run_until_terminal()
    assert first_run.status is ProductionRunStatus.READY
    assert len(first_script.calls) == 2

    second, second_script = _configure(production_scenario_factory, urls, core_urls=urls)
    await second.start()
    second_run = await second.run_until_terminal()
    assert second_run.status is ProductionRunStatus.READY
    assert second_script.calls == []

    extraction = await _canonical_extraction(second, second_run.id)
    assert [source.reuse_state for source in extraction.sources] == [
        ExtractionReuseState.REUSED,
        ExtractionReuseState.REUSED,
    ]
    artifact = await _current_artifact(second, ProductionArtifactStage.EXTRACTION)
    assert artifact is not None
    assert artifact.metadata["reused_source_count"] == 2
    assert artifact.metadata["fresh_source_count"] == 0


@pytest.mark.asyncio
async def test_submission_ambiguity_stops_for_review_without_replay(
    production_scenario_factory: ScenarioFactory,
) -> None:
    urls = _urls(1, namespace="ambiguity")
    scenario, script = _configure(production_scenario_factory, urls, core_urls=urls)
    script.ambiguity = ModelSubmissionReconciliationRequiredError(
        "The provider may have received the request", details={"request_id": "opaque"}
    )

    await scenario.start()
    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.NEEDS_REVIEW
    assert run.error_code == "model_submission_reconciliation_required"
    assert len(script.calls) == 1


@pytest.mark.asyncio
async def test_retry_without_change_reuses_the_extraction_artifact(
    production_scenario_factory: ScenarioFactory,
) -> None:
    urls = _urls(2, namespace="retry")
    scenario, script = _configure(production_scenario_factory, urls, core_urls=urls)
    await scenario.start()
    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.READY, (run.error_code, run.error_details)
    calls = len(script.calls)
    initial = await _current_artifact(scenario, ProductionArtifactStage.EXTRACTION)
    assert initial is not None

    await _retry_extraction(scenario)
    retried = await scenario.run_until_terminal()
    assert retried.status is ProductionRunStatus.READY
    assert len(script.calls) == calls
    current = await _current_artifact(scenario, ProductionArtifactStage.EXTRACTION)
    assert current is not None
    assert current.version == initial.version + 1
    assert current.metadata["reused_source_count"] == 2
    assert current.metadata["fresh_source_count"] == 0
    extraction = await _canonical_extraction(scenario, retried.id)
    assert [source.reuse_state for source in extraction.sources] == [
        ExtractionReuseState.REUSED,
        ExtractionReuseState.REUSED,
    ]

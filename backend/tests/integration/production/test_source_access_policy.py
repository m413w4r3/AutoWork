"""Cross-stage business contract for Q1 archive and Q2 source access."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pytest

from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.domain.collection import CollectionState
from cti_app.domain.model_runs import ModelRunStatus, ModelSubmissionState
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.integrations.models import BridgeTransportError

from .support import ProductionScenario

pytestmark = pytest.mark.integration

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]

S1 = "https://example.test/source-1"
S2 = "https://example.test/source-2"
S3 = "https://example.test/source-3"
BOOTSTRAP = "https://example.test/bootstrap"

ARCHIVE_S1 = "ARCHIVE_S1 ExampleRAT live-success.security-lab.io"
ARCHIVE_S2 = "ARCHIVE_S2 ExampleRAT archive-fallback.security-lab.io"
ARCHIVE_S3 = "ARCHIVE_S3 ExampleRAT batch-success.security-lab.io"
EMPTY_ARCHIVE = ""

SYNTHESIS = "ExampleRAT activity is documented by the selected source [S1]."


@dataclass(frozen=True, slots=True)
class ReloadedProduction:
    run: Any
    snapshot: Any
    artifacts: tuple[Any, ...]
    collections: tuple[Any, ...]
    documents: tuple[Any, ...]
    model_runs: dict[Any, Any]

    @property
    def artifacts_by_stage(self) -> dict[str, Any]:
        return {artifact.stage.value: artifact for artifact in self.artifacts}

    @property
    def progress(self) -> dict[str, Any]:
        assert isinstance(self.run.extraction_progress, dict)
        return self.run.extraction_progress


def _source_specs(
    bodies: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    return {
        url: {"status": 200, "mime": "text/plain", "body": body} for url, body in bodies.items()
    }


def _reference_report(urls: Sequence[str]) -> str:
    lines = [
        "# REFERENCES",
        "editorial-title: [Publication] ExampleRAT activity",
        "",
    ]
    for index, url in enumerate(urls, start=1):
        role = "primary" if index == 1 else "independent"
        lines.extend(
            (
                f"## SOURCE S{index}",
                f"title: ExampleRAT source {index}",
                f"url: {url}",
                f"publisher: Lab {index}",
                f"published-at: 2026-08-{10 + index:02d}",
                f"role: {role}",
                "",
            )
        )
    source_ids = ", ".join(f"S{index}" for index in range(1, len(urls) + 1))
    lines.extend(
        (
            "## EVENT R1",
            "date: 2026-08-15",
            f"sources: {source_ids}",
            "text: ExampleRAT activity is documented by these publications.",
        )
    )
    return "\n".join(lines)


def _configure(
    factory: ScenarioFactory,
    bodies: Mapping[str, str],
    *,
    report_urls: Sequence[str],
    core_urls: Sequence[str] | None = None,
) -> ProductionScenario:
    scenario = factory(_source_specs(bodies))
    if core_urls is not None:
        scenario.restrict_core_sources(core_urls)
    scenario.model.script.references(_reference_report(report_urls))
    scenario.model.script.synthesis(SYNTHESIS)
    return scenario


async def _reload(scenario: ProductionScenario) -> ReloadedProduction:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        run = await uow.subject_production_runs.get(scenario.run_id)
        assert run is not None
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        artifacts = tuple(await uow.production_artifacts.list_for_run(run.id))
        collections = tuple(await uow.source_collections.list_for_subject(run.subject_id))
        documents = tuple(await uow.source_documents.list_for_subject(run.subject_id))
        model_runs: dict[Any, Any] = {}
        for call in scenario.model.calls:
            if call.model_run_id is not None:
                model_runs.setdefault(
                    call.model_run_id,
                    await uow.model_runs.get(call.model_run_id),
                )
    assert snapshot is not None
    assert all(model_run is not None for model_run in model_runs.values())
    return ReloadedProduction(
        run=run,
        snapshot=snapshot,
        artifacts=artifacts,
        collections=collections,
        documents=documents,
        model_runs=model_runs,
    )


def _diagnostic_events(scenario: ProductionScenario) -> list[dict[str, Any]]:
    path = scenario.blob_root.parent / "diagnostics" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _assert_common(
    scenario: ProductionScenario,
    state: ReloadedProduction,
    *,
    status: SubjectProductionStatus,
    current_stage: SubjectProductionStage,
    report_source_ids: Sequence[str],
    collection_urls: Sequence[str],
    model_call_count: int,
    expected_stages: Sequence[str],
    verify_document_hashes: bool = True,
) -> None:
    assert state.run.status is status, (
        state.run.status,
        state.run.current_stage,
        state.run.error_code,
        state.run.error_details,
        [call.stage for call in scenario.model.calls],
    )
    assert state.run.current_stage is current_stage
    assert state.snapshot.production_run_id == state.run.id
    assert state.snapshot.core_sources
    assert {collection.canonical_url for collection in state.collections} == set(collection_urls)
    assert all(collection.state is CollectionState.ARCHIVED for collection in state.collections)
    assert {document.final_url for document in state.documents} == set(collection_urls)
    assert len(scenario.collection_transport.requests) == len(collection_urls)
    assert [request.url for request in scenario.collection_transport.requests] == list(
        collection_urls
    )

    actual_calls = [
        (call.stage, call.source_urls, call.request.metadata.get("access_mode"))
        for call in scenario.model.calls
    ]
    assert len(actual_calls) == model_call_count, actual_calls
    assert [call.stage for call in scenario.model.calls] == list(expected_stages)
    assert all(call.model_run_id is not None for call in scenario.model.calls)
    assert all(
        state.model_runs[call.model_run_id].status
        in {ModelRunStatus.SUCCEEDED, ModelRunStatus.FAILED, ModelRunStatus.NEEDS_REVIEW}
        for call in scenario.model.calls
        if call.model_run_id is not None
    )

    for document in state.documents:
        assert document.decoded_blob_id is not None
        content = await _read_blob(scenario, document.decoded_blob_id)
        if verify_document_hashes:
            assert document.decoded_sha256 == hashlib.sha256(content).hexdigest()

    progress = state.progress
    progress_sources = {item["source_id"]: item for item in progress["sources"]}
    assert set(progress_sources) == set(report_source_ids)
    assert progress["completed_sources"] == sum(
        item["status"] in {"cached", "succeeded"} for item in progress_sources.values()
    )
    assert progress["skipped_sources"] == sum(
        item["status"] == "skipped" for item in progress_sources.values()
    )


async def _read_blob(scenario: ProductionScenario, blob_id: Any) -> bytes:
    return await scenario.artifact_store.read_bytes(blob_id)


async def _assert_documents_hashes(
    scenario: ProductionScenario,
    state: ReloadedProduction,
    *,
    verify: bool,
) -> None:
    for document in state.documents:
        assert document.decoded_blob_id is not None
        content = await _read_blob(scenario, document.decoded_blob_id)
        if verify:
            assert document.decoded_sha256 == hashlib.sha256(content).hexdigest()


def _assert_extraction_stages(
    state: ReloadedProduction,
    expected: set[str],
) -> None:
    assert set(state.artifacts_by_stage) == expected
    assert all(artifact.status is ProductionArtifactStatus.VERIFIED for artifact in state.artifacts)


def _assert_progress_statuses(
    state: ReloadedProduction,
    expected: Mapping[str, str],
) -> None:
    actual = {item["source_id"]: item["status"] for item in state.progress["sources"]}
    assert actual == dict(expected)


def _verification_diagnostics(state: ReloadedProduction) -> dict[str, Any]:
    artifact = state.artifacts_by_stage.get(ProductionArtifactStage.EXTRACTION.value)
    if artifact is not None:
        return cast(dict[str, Any], artifact.metadata["deterministic_verification"])
    return state.run.error_details or {}


@pytest.mark.asyncio
async def test_live_success_is_live_first_and_never_inlines_archive(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {S1: ARCHIVE_S1},
        report_urls=(S1,),
    )
    scenario.model.script.q2(
        source_url=S1,
        access_mode="live_url",
        response=(
            "FACT malware\n- ExampleRAT\nIOC confirmed domain\n- live-success.security-lab.io"
        ),
    )

    await scenario.start()
    run = await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        report_source_ids=("S1",),
        collection_urls=(S1,),
        model_call_count=3,
        expected_stages=("references", "extraction", "synthesis"),
    )
    assert run.status is SubjectProductionStatus.READY
    _assert_extraction_stages(
        state,
        {stage.value for stage in ProductionArtifactStage},
    )
    _assert_progress_statuses(state, {"S1": "succeeded"})

    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert len(q2_calls) == 1
    assert q2_calls[0].request.web_search is True
    assert q2_calls[0].request.prompt_template_id == "production-q2-url"
    assert q2_calls[0].request.metadata.get("access_mode") is None
    assert ARCHIVE_S1 not in q2_calls[0].request.text
    assert state.run.error_code is None
    assert state.run.reconciliation is None
    assert _verification_diagnostics(state)["source_skips"] == {}
    assert _verification_diagnostics(state).get("source_failures", {}) == {}


@pytest.mark.asyncio
async def test_live_unavailable_uses_one_verified_archive_fallback(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {S1: ARCHIVE_S1},
        report_urls=(S1,),
    )
    scenario.model.script.q2(source_url=S1, access_mode="live_url", response="UNAVAILABLE")
    scenario.model.script.q2(
        source_url=S1,
        access_mode="archive_fallback",
        response="IOC confirmed domain\n- live-success.security-lab.io",
    )

    await scenario.start()
    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        report_source_ids=("S1",),
        collection_urls=(S1,),
        model_call_count=4,
        expected_stages=("references", "extraction", "extraction", "synthesis"),
    )
    _assert_extraction_stages(state, {stage.value for stage in ProductionArtifactStage})
    _assert_progress_statuses(state, {"S1": "succeeded"})

    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert len(q2_calls) == 2
    live_call, fallback_call = q2_calls
    assert live_call.request.web_search is True
    assert fallback_call.request.web_search is False
    assert fallback_call.request.prompt_template_id == "production-q2-url-archive-fallback"
    assert fallback_call.request.metadata["access_mode"] == "archive_fallback"
    assert ARCHIVE_S1 in fallback_call.request.text
    assert ARCHIVE_S1 not in live_call.request.text
    assert (
        fallback_call.request.metadata["source_content_sha256"]
        == hashlib.sha256(ARCHIVE_S1.encode()).hexdigest()
    )

    extraction = await scenario.artifact_store.read_json(
        state.artifacts_by_stage[ProductionArtifactStage.EXTRACTION.value].canonical_blob_id
    )
    assert extraction["items"][0]["value"] == "live-success.security-lab.io"
    assert extraction["items"][0]["source_ids"] == ["S1"]
    assert state.model_runs[fallback_call.model_run_id].parameters["q2_access_mode"] == (
        "archive_fallback"
    )
    fallback_events = [
        event
        for event in _diagnostic_events(scenario)
        if event.get("event") == "q2.source.archive_fallback_completed"
    ]
    assert len(fallback_events) == 1
    assert (
        fallback_events[0]["source_content_sha256"]
        == hashlib.sha256(ARCHIVE_S1.encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_live_unavailable_with_empty_archive_is_non_blocking_skip(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {S1: EMPTY_ARCHIVE},
        report_urls=(S1,),
    )
    scenario.model.script.q2(source_url=S1, access_mode="live_url", response="UNAVAILABLE")

    await scenario.start()
    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        report_source_ids=("S1",),
        collection_urls=(S1,),
        model_call_count=3,
        expected_stages=("references", "extraction", "synthesis"),
    )
    _assert_extraction_stages(state, {stage.value for stage in ProductionArtifactStage})
    _assert_progress_statuses(state, {"S1": "skipped"})
    diagnostics = _verification_diagnostics(state)
    assert diagnostics["source_skips"]["S1"]["blocking"] is False
    assert diagnostics["source_skips"]["S1"]["archive_error_code"] == (
        "q2_source_evidence_unavailable"
    )
    assert state.run.error_code is None
    assert "q2_source_coverage_failed" not in str(state.run.error_details)
    assert not any(
        call.request.metadata.get("access_mode") == "archive_fallback"
        for call in scenario.model.calls
        if call.stage == "extraction"
    )
    assert any(
        event.get("event") == "q2.source.skipped"
        and event.get("archive_reason") == "Archived source text is empty"
        for event in _diagnostic_events(scenario)
    )


@pytest.mark.asyncio
async def test_live_unavailable_with_sha_mismatch_skips_without_using_untrusted_blob(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {S1: ARCHIVE_S1},
        report_urls=(S1,),
    )
    scenario.model.script.q2(source_url=S1, access_mode="live_url", response="UNAVAILABLE")

    await scenario.start()
    assert await scenario.runner.run_next()
    assert await scenario.runner.run_next()
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        documents = list(await uow.source_documents.list_for_subject(scenario.subject.id))
        document = next(item for item in documents if item.final_url == S1)
        document.decoded_sha256 = "f" * 64
        await uow.source_documents.save(document)
        await uow.commit()

    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=False)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        report_source_ids=("S1",),
        collection_urls=(S1,),
        model_call_count=3,
        expected_stages=("references", "extraction", "synthesis"),
        verify_document_hashes=False,
    )
    document = state.documents[0]
    assert document.decoded_sha256 == "f" * 64
    assert document.decoded_blob_id is not None
    assert hashlib.sha256(await _read_blob(scenario, document.decoded_blob_id)).hexdigest() != (
        document.decoded_sha256
    )
    _assert_extraction_stages(state, {stage.value for stage in ProductionArtifactStage})
    _assert_progress_statuses(state, {"S1": "skipped"})
    assert _verification_diagnostics(state)["source_skips"]["S1"]["blocking"] is False
    assert not any(
        call.request.metadata.get("access_mode") == "archive_fallback"
        for call in scenario.model.calls
        if call.stage == "extraction"
    )
    assert any(
        event.get("event") == "q2.source.skipped"
        and event.get("archive_reason") == "Archived decoded blob integrity check failed"
        for event in _diagnostic_events(scenario)
    )


@pytest.mark.asyncio
async def test_partial_ioc_rules_batch_falls_back_only_for_unavailable_source(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {BOOTSTRAP: "bootstrap", S1: ARCHIVE_S1, S2: ARCHIVE_S2, S3: ARCHIVE_S3},
        report_urls=(S1, S2, S3),
        core_urls=(BOOTSTRAP,),
    )
    scenario.model.script.q2(
        source_url=S1,
        access_mode="live_url",
        response="IOC confirmed domain\n- batch-success.security-lab.io",
    )
    scenario.model.script.q2(source_url=S2, access_mode="live_url", response="UNAVAILABLE")
    scenario.model.script.q2(
        source_url=S3,
        access_mode="live_url",
        response="IOC confirmed domain\n- batch-success.security-lab.io",
    )
    scenario.model.script.q2(
        source_url=S2,
        access_mode="archive_fallback",
        response="IOC confirmed domain\n- archive-fallback.security-lab.io",
    )

    await scenario.start()
    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        report_source_ids=("S1", "S2", "S3"),
        collection_urls=(BOOTSTRAP, S1, S2, S3),
        model_call_count=4,
        expected_stages=("references", "extraction", "extraction", "synthesis"),
    )
    _assert_extraction_stages(state, {stage.value for stage in ProductionArtifactStage})
    _assert_progress_statuses(
        state,
        {"S1": "succeeded", "S2": "succeeded", "S3": "succeeded"},
    )
    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert len(q2_calls) == 2
    batch_call, fallback_call = q2_calls
    assert batch_call.request.prompt_template_id == "production-q2-ioc-batch"
    assert batch_call.source_urls == (S1, S2, S3)
    assert batch_call.request.web_search is True
    assert fallback_call.source_url == S2
    assert fallback_call.request.web_search is False
    assert fallback_call.request.metadata["access_mode"] == "archive_fallback"
    assert ARCHIVE_S2 in fallback_call.request.text
    assert all(
        call.source_url not in {S1, S3} for call in q2_calls[1:] if call.source_url is not None
    )
    assert state.model_runs[batch_call.model_run_id].parameters["q2_execution_kind"] == "batch"
    assert state.model_runs[fallback_call.model_run_id].parameters["q2_access_mode"] == (
        "archive_fallback"
    )
    assert _verification_diagnostics(state)["source_skips"] == {}
    assert _verification_diagnostics(state).get("source_failures", {}) == {}


@pytest.mark.asyncio
async def test_partial_ioc_rules_batch_without_archive_skips_only_that_source(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {BOOTSTRAP: "bootstrap", S1: ARCHIVE_S1, S2: EMPTY_ARCHIVE, S3: ARCHIVE_S3},
        report_urls=(S1, S2, S3),
        core_urls=(BOOTSTRAP,),
    )
    for url, value in (
        (S1, "batch-success.security-lab.io"),
        (S3, "batch-success.security-lab.io"),
    ):
        scenario.model.script.q2(
            source_url=url,
            access_mode="live_url",
            response=f"IOC confirmed domain\n- {value}",
        )
    scenario.model.script.q2(source_url=S2, access_mode="live_url", response="UNAVAILABLE")

    await scenario.start()
    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        report_source_ids=("S1", "S2", "S3"),
        collection_urls=(BOOTSTRAP, S1, S2, S3),
        model_call_count=3,
        expected_stages=("references", "extraction", "synthesis"),
    )
    _assert_extraction_stages(state, {stage.value for stage in ProductionArtifactStage})
    _assert_progress_statuses(
        state,
        {"S1": "succeeded", "S2": "skipped", "S3": "succeeded"},
    )
    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert len(q2_calls) == 1
    assert q2_calls[0].request.prompt_template_id == "production-q2-ioc-batch"
    assert q2_calls[0].request.web_search is True
    assert _verification_diagnostics(state)["source_skips"]["S2"]["blocking"] is False
    assert state.run.error_code is None
    assert not any(
        call.request.metadata.get("access_mode") == "archive_fallback" for call in q2_calls
    )


@pytest.mark.asyncio
async def test_retryable_bridge_error_stays_infrastructure_and_never_falls_back(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {S1: ARCHIVE_S1},
        report_urls=(S1,),
    )
    scenario.model.script.q2(
        source_url=S1,
        access_mode="live_url",
        response=BridgeTransportError(
            "bridge_timeout",
            "provider timeout before submission",
            retryable=True,
            phase="pre_submission",
            submission_state="pre_submission",
        ),
    )

    await scenario.start()
    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.NEEDS_REVIEW,
        current_stage=SubjectProductionStage.EXTRACTION,
        report_source_ids=("S1",),
        collection_urls=(S1,),
        model_call_count=7,
        expected_stages=(
            "references",
            "extraction",
            "extraction",
            "extraction",
            "extraction",
            "extraction",
            "extraction",
        ),
    )
    _assert_extraction_stages(
        state,
        {ProductionArtifactStage.REFERENCES.value},
    )
    _assert_progress_statuses(state, {"S1": "failed"})
    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert len(q2_calls) == 6
    assert all(call.request.web_search is True for call in q2_calls)
    assert all(call.request.metadata.get("access_mode") is None for call in q2_calls)
    q2_runs = [state.model_runs[call.model_run_id] for call in q2_calls]
    assert len({run.id for run in q2_runs}) == 2
    assert all(run.status is ModelRunStatus.FAILED for run in q2_runs)
    assert all(run.error_code == "bridge_timeout" for run in q2_runs)
    assert all(run.submission_attempt == 3 for run in q2_runs)
    assert all(run.submission_state is ModelSubmissionState.NOT_SUBMITTED for run in q2_runs)
    async with scenario.uow_factory() as uow:
        batch_item = await uow.edition_production_batch_items.get_by_run(state.run.id)
    assert batch_item is not None
    assert batch_item.auto_recovery_count == 1
    assert state.run.error_code == "bridge_timeout"
    assert state.run.error_details is not None
    failure = state.run.error_details["source_failures"]["S1"]
    assert failure["failure_class"] == "global_transient_pre_submission"
    assert failure["contributes_to_coverage"] is False
    assert failure["retryable"] is True
    assert state.run.error_details.get("source_skips", {}) == {}
    assert "q2_source_coverage_failed" not in str(state.run.error_details)
    assert ProductionRecoveryPolicyV1.disposition_for_run(state.run) is (
        ProductionRecoveryPolicyV1.AUTO
    )
    assert ProductionRecoveryPolicyV1.current_stage_retry_recommended(state.run)
    assert not any(call.stage == "synthesis" for call in scenario.model.calls)


@pytest.mark.asyncio
async def test_reconciliation_required_is_not_replayed_or_fallbacked(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {S1: ARCHIVE_S1},
        report_urls=(S1,),
    )
    scenario.model.script.q2(
        source_url=S1,
        access_mode="live_url",
        response=BridgeTransportError(
            "bridge_timeout",
            "provider received the prompt but no final answer was returned",
            retryable=True,
            phase="generation",
            submission_state="post_submission",
            bridge_run_id="bridge-q2-s1",
        ),
    )

    await scenario.start()
    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.NEEDS_REVIEW,
        current_stage=SubjectProductionStage.EXTRACTION,
        report_source_ids=("S1",),
        collection_urls=(S1,),
        model_call_count=2,
        expected_stages=("references", "extraction"),
    )
    _assert_extraction_stages(state, {ProductionArtifactStage.REFERENCES.value})
    _assert_progress_statuses(state, {"S1": "needs_review"})
    q2_call = next(call for call in scenario.model.calls if call.stage == "extraction")
    q2_run = state.model_runs[q2_call.model_run_id]
    assert q2_run.status is ModelRunStatus.NEEDS_REVIEW
    assert q2_run.error_code == "model_submission_reconciliation_required"
    assert q2_run.submission_state is ModelSubmissionState.SUBMITTED_OR_UNKNOWN
    assert state.run.error_code == "model_submission_reconciliation_required"
    assert state.run.reconciliation is not None
    assert state.run.reconciliation.model_run_id == q2_run.id
    assert state.run.reconciliation.bridge_response_id == "bridge-q2-s1"
    assert state.run.reconciliation.phase == "reconciliation"
    assert ProductionRecoveryPolicyV1.disposition_for_run(state.run) is (
        ProductionRecoveryPolicyV1.MANUAL_ONLY
    )
    assert not ProductionRecoveryPolicyV1.current_stage_retry_recommended(state.run)
    assert not any(
        call.request.metadata.get("access_mode") == "archive_fallback"
        for call in scenario.model.calls
    )
    assert not any(call.stage == "synthesis" for call in scenario.model.calls)


@pytest.mark.asyncio
async def test_invalid_live_q2_output_is_parser_failure_not_source_unavailable(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {S1: ARCHIVE_S1},
        report_urls=(S1,),
    )
    scenario.model.script.q2(source_url=S1, access_mode="live_url", response="not Q2 markdown")

    await scenario.start()
    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.NEEDS_REVIEW,
        current_stage=SubjectProductionStage.EXTRACTION,
        report_source_ids=("S1",),
        collection_urls=(S1,),
        model_call_count=2,
        expected_stages=("references", "extraction"),
    )
    _assert_extraction_stages(state, {ProductionArtifactStage.REFERENCES.value})
    _assert_progress_statuses(state, {"S1": "failed"})
    q2_call = next(call for call in scenario.model.calls if call.stage == "extraction")
    q2_run = state.model_runs[q2_call.model_run_id]
    assert q2_run.status is ModelRunStatus.SUCCEEDED
    assert q2_run.raw_output_sha256 == hashlib.sha256(b"not Q2 markdown").hexdigest()
    assert state.run.error_code == "q2_source_coverage_failed"
    assert state.run.error_details is not None
    failure = state.run.error_details["source_failures"]["S1"]
    assert failure["error_code"] == "source_content_invalid"
    assert failure["failure_class"] == "source_content_failure"
    assert failure["contributes_to_coverage"] is True
    assert state.run.error_details.get("source_skips", {}) == {}
    assert ProductionRecoveryPolicyV1.disposition_for_run(state.run) is (
        ProductionRecoveryPolicyV1.MANUAL_ONLY
    )
    assert q2_call.request.metadata.get("access_mode") is None
    assert not any(
        call.request.metadata.get("access_mode") == "archive_fallback"
        for call in scenario.model.calls
    )
    parse_events = [
        event
        for event in _diagnostic_events(scenario)
        if event.get("event") == "parse.result" and event.get("stage") == "extraction"
    ]
    assert parse_events
    assert parse_events[-1]["errors"] == ["q2_compact_sections_missing"]


@pytest.mark.asyncio
async def test_archive_fallback_still_passes_the_source_evidence_gate(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(
        production_scenario_factory,
        {S1: "ARCHIVE_ONLY trusted.security-lab.io"},
        report_urls=(S1,),
    )
    scenario.model.script.q2(source_url=S1, access_mode="live_url", response="UNAVAILABLE")
    scenario.model.script.q2(
        source_url=S1,
        access_mode="archive_fallback",
        response="IOC confirmed domain\n- foreign.security-lab.io",
    )

    await scenario.start()
    await scenario.run_until_terminal()
    state = await _reload(scenario)
    await _assert_documents_hashes(scenario, state, verify=True)
    await _assert_common(
        scenario,
        state,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        report_source_ids=("S1",),
        collection_urls=(S1,),
        model_call_count=4,
        expected_stages=("references", "extraction", "extraction", "synthesis"),
    )
    _assert_extraction_stages(state, {stage.value for stage in ProductionArtifactStage})
    _assert_progress_statuses(state, {"S1": "succeeded"})
    extraction = await scenario.artifact_store.read_json(
        state.artifacts_by_stage[ProductionArtifactStage.EXTRACTION.value].canonical_blob_id
    )
    assert extraction["items"] == []
    diagnostics = _verification_diagnostics(state)
    assert diagnostics["source_skips"] == {}
    assert diagnostics["q2_source_evidence_rejection_groups"] == [
        {
            "source_id": "S1",
            "batch_id": None,
            "artifact_type": "domain",
            "rejection_count": 1,
            "reason_code": "source_evidence_missing",
        }
    ]
    assert any(
        warning == "q2_source_evidence_rejected:S1:domain:count=1:reason=source_evidence_missing"
        for warning in state.artifacts_by_stage[ProductionArtifactStage.EXTRACTION.value].metadata[
            "warnings"
        ]
    )
    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert len(q2_calls) == 2
    assert q2_calls[0].request.web_search is True
    assert q2_calls[1].request.web_search is False
    assert "trusted.security-lab.io" in q2_calls[1].request.text
    assert all(item["value"] != "foreign.security-lab.io" for item in extraction["items"])

"""Business-level recovery, retry and idempotence contracts for Production."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from cti_app.api.production import _create_and_start_run
from cti_app.application.jobs import JobStatus
from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageChain,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_reconciliation import ProductionReconciliationService
from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.model_runs import ModelRunStatus, ModelSubmissionState
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionBatchStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.integrations.models import BridgeTransportError

from .support import ProductionScenario

pytestmark = pytest.mark.integration

ScenarioFactory = Callable[
    [Mapping[str, Mapping[str, object]]], ProductionScenario
]


def _urls(count: int) -> tuple[str, ...]:
    return tuple(f"https://example.test/source-{index}" for index in range(1, count + 1))


def _source_specs(urls: tuple[str, ...]) -> dict[str, dict[str, object]]:
    return {
        url: {
            "status": 200,
            "mime": "text/plain",
            "body": (
                f"ExampleRAT source {index} source-{index}.security-lab.io "
                "was archived for this business test."
            ),
        }
        for index, url in enumerate(urls, start=1)
    }


def _references_response(urls: tuple[str, ...]) -> str:
    lines = [
        "# REFERENCES",
        "editorial-title: [Publication] ExampleRAT activity",
        "",
    ]
    for index, url in enumerate(urls, start=1):
        lines.extend(
            (
                f"## SOURCE S{index}",
                f"title: ExampleRAT source {index}",
                f"url: {url}",
                f"publisher: Lab {index}",
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
            "text: ExampleRAT activity is documented by the selected publications.",
        )
    )
    return "\n".join(lines)


def _q2_response(index: int) -> str:
    return (
        "FACT malware\n"
        "- ExampleRAT :: The source documents the ExampleRAT family.\n\n"
        "IOC confirmed domain\n"
        f"- source-{index}.security-lab.io :: Infrastructure observed in source {index}."
    )


def _synthesis_response(urls: tuple[str, ...]) -> str:
    citations = " ".join(f"[S{index}]" for index in range(1, len(urls) + 1))
    return f"ExampleRAT activity is documented by the selected reports {citations}."


def _configured(
    factory: ScenarioFactory,
    count: int = 1,
) -> tuple[ProductionScenario, tuple[str, ...]]:
    urls = _urls(count)
    scenario = factory(_source_specs(urls))
    # The country code comes from ProductionScenario's shared allocator: the
    # integration database is session-scoped and editions are unique on
    # (country_code, period_start, period_end), so it must not be reassigned
    # here.
    scenario.edition.country = f"Recovery Test {scenario.edition.country_code}"
    scenario.model.script.references(_references_response(urls))
    scenario.model.script.synthesis(_synthesis_response(urls))
    for index, url in enumerate(urls, start=1):
        scenario.model.script.q2(
            source_url=url,
            access_mode="live_url",
            response=_q2_response(index),
        )
    return scenario, urls


async def _state(
    scenario: ProductionScenario,
) -> tuple[SubjectProductionRun, list[Any], Any, Any]:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        run = await uow.subject_production_runs.get(scenario.run_id)
        artifacts = list(await uow.production_artifacts.list_for_run(scenario.run_id))
        item = await uow.edition_production_batch_items.get_by_run(scenario.run_id)
        batch = await uow.edition_production_batches.get(item.batch_id) if item else None
    assert run is not None
    return run, artifacts, item, batch


async def _jobs_for_run(scenario: ProductionScenario) -> list[Any]:
    assert scenario.run_id is not None
    jobs = await scenario.jobs.list_for_aggregate("subject", scenario.subject.id)
    return [job for job in jobs if str(job.input_parameters.get("run_id")) == str(scenario.run_id)]


async def _dispatch_stage(
    scenario: ProductionScenario,
    run: SubjectProductionRun,
    stage: SubjectProductionStage,
) -> UUID:
    chain = ProductionStageChain()
    chain.bind(scenario.jobs, scenario.runner)
    job_id = await chain.submit(
        run=run,
        stage=stage,
        correlation_id="operator-recovery-test",
        actor_id="operator-test",
    )
    assert job_id is not None
    return job_id


async def _assert_artifact_invariants(
    scenario: ProductionScenario,
    artifacts: list[Any],
) -> None:
    assert all(artifact.status in ProductionArtifactStatus for artifact in artifacts)
    for stage in ProductionArtifactStage:
        stage_artifacts = [artifact for artifact in artifacts if artifact.stage is stage]
        assert [artifact.version for artifact in stage_artifacts] == sorted(
            {artifact.version for artifact in stage_artifacts}
        )
        active = [
            artifact
            for artifact in stage_artifacts
            if artifact.status is not ProductionArtifactStatus.STALE
        ]
        assert len(active) <= 1
        assert all(artifact.status is ProductionArtifactStatus.VERIFIED for artifact in active)
        if active:
            async with scenario.uow_factory() as uow:
                current = await uow.production_artifacts.get_current(
                    artifacts[0].production_run_id, stage.value
                )
            assert current is not None
            assert current.id == active[0].id


def _diagnostic_events(scenario: ProductionScenario) -> list[dict[str, Any]]:
    path = scenario.blob_root.parent / "diagnostics" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _extraction_source_model_ids(payload: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue
        source_ids = item.get("source_ids", [])
        model_run_ids = item.get("model_run_ids", [])
        if not isinstance(source_ids, list) or not isinstance(model_run_ids, list):
            continue
        for source_id in source_ids:
            if isinstance(source_id, str):
                result.setdefault(source_id, set()).update(
                    value for value in model_run_ids if isinstance(value, str)
                )
    return result


@pytest.mark.asyncio
async def test_retryable_source_recovery_reuses_thirteen_checkpoints(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, urls = _configured(production_scenario_factory, count=14)
    original_response_for = scenario.model.script.response_for
    transient_failures = [
        BridgeTransportError(
            "bridge_unreachable",
            "the bridge was unavailable before submission",
            retryable=True,
            phase="pre_submission",
            submission_state="pre_submission",
        )
        for _ in range(PRODUCTION_STAGE_MAX_ATTEMPTS)
    ]

    def fail_s14_then_recover(request: Any) -> str | Exception:
        if (
            request.metadata.get("source_url") == urls[-1]
            and request.metadata.get("access_mode") is None
            and transient_failures
        ):
            return transient_failures.pop(0)
        return original_response_for(request)

    with patch.object(
        scenario.model.script,
        "response_for",
        side_effect=fail_s14_then_recover,
    ):
        await scenario.start()
        run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.READY
    assert run.pipeline_generation == 1
    assert run.current_stage is SubjectProductionStage.ASSEMBLY

    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    first_model_ids = {
        call.source_url: call.model_run_id
        for call in q2_calls
        if call.source_url != urls[-1]
    }
    assert len(first_model_ids) == 13
    s14_calls = [call for call in q2_calls if call.source_url == urls[-1]]
    assert len(s14_calls) == 4
    assert len({call.model_run_id for call in s14_calls}) == 2

    persisted_run, artifacts, item, batch = await _state(scenario)
    assert persisted_run.pipeline_generation == 1
    assert item is not None and item.auto_recovery_count == 1
    assert batch is not None and batch.status is ProductionBatchStatus.COMPLETED
    assert batch.phase is ProductionBatchPhase.REVIEW
    await _assert_artifact_invariants(scenario, artifacts)

    extraction = next(
        artifact for artifact in artifacts if artifact.stage is ProductionArtifactStage.EXTRACTION
    )
    assert extraction.status is ProductionArtifactStatus.VERIFIED
    assert extraction.canonical_blob_id is not None
    payload = await scenario.artifact_store.read_json(extraction.canonical_blob_id)
    ids_by_source = _extraction_source_model_ids(payload)
    for index, url in enumerate(urls[:-1], start=1):
        assert str(first_model_ids[url]) in ids_by_source[f"S{index}"]
        source_item = next(
            item
            for item in payload["items"]
            if item["value"] == f"source-{index}.security-lab.io"
        )
        assert set(source_item["model_run_ids"]) == {str(first_model_ids[url])}
    s14_item = next(
        item for item in payload["items"] if item["value"] == "source-14.security-lab.io"
    )
    assert set(s14_item["model_run_ids"]) == {str(s14_calls[-1].model_run_id)}

    diagnostics = extraction.metadata["deterministic_verification"]
    assert diagnostics["cache_hits"] == 13
    assert diagnostics["model_calls_avoided"] == 13
    reused = [
        event
        for event in _diagnostic_events(scenario)
        if event.get("event") == "q2.source.reused"
    ]
    assert {event["source_id"] for event in reused} >= {f"S{index}" for index in range(1, 14)}

    async with scenario.uow_factory() as uow:
        for model_run_id in first_model_ids.values():
            model_run = await uow.model_runs.get(model_run_id)
            assert model_run is not None
            assert model_run.status is ModelRunStatus.SUCCEEDED
        all_runs = await uow.subject_production_runs.list_for_edition(run.edition_id)
    assert len(all_runs) == 1


@pytest.mark.asyncio
async def test_terminal_source_failure_does_not_schedule_a_next_generation(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, urls = _configured(production_scenario_factory, count=14)
    scenario.model.script.q2(
        source_url=urls[-1],
        access_mode="live_url",
        response="not Q2 markdown",
    )

    await scenario.start()
    run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert run.current_stage is SubjectProductionStage.EXTRACTION
    assert run.pipeline_generation == 0
    assert run.error_code == "q2_source_coverage_failed"
    persisted_run, artifacts, item, batch = await _state(scenario)
    assert persisted_run.id == run.id
    assert item is not None and item.auto_recovery_count == 0
    assert batch is not None
    assert batch.status is ProductionBatchStatus.COMPLETED_WITH_ISSUES
    assert batch.phase is ProductionBatchPhase.REVIEW
    assert [artifact.stage for artifact in artifacts] == [ProductionArtifactStage.REFERENCES]
    await _assert_artifact_invariants(scenario, artifacts)

    failures = run.error_details["source_failures"] if run.error_details else {}
    assert failures["S14"]["retryable"] is False
    jobs = await _jobs_for_run(scenario)
    assert all(job.input_parameters["pipeline_generation"] == 0 for job in jobs)
    assert not any(
        job.input_parameters["pipeline_generation"] > 0 for job in jobs
    )
    assert not any(call.stage == "synthesis" for call in scenario.model.calls)


@pytest.mark.asyncio
async def test_mixed_source_retryability_never_opens_global_recovery(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, urls = _configured(production_scenario_factory, count=14)
    original_response_for = scenario.model.script.response_for
    retryable_failure = BridgeTransportError(
        "bridge_unreachable",
        "the bridge was unavailable before submission",
        retryable=True,
        phase="pre_submission",
        submission_state="pre_submission",
    )

    def mixed_failures(request: Any) -> str | Exception:
        if request.metadata.get("source_url") == urls[-2]:
            return "not Q2 markdown"
        if request.metadata.get("source_url") == urls[-1]:
            return retryable_failure
        return original_response_for(request)

    with patch.object(scenario.model.script, "response_for", side_effect=mixed_failures):
        await scenario.start()
        run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert run.pipeline_generation == 0
    assert run.current_stage is SubjectProductionStage.EXTRACTION
    assert run.error_code == "bridge_unreachable"
    assert run.error_details is not None
    failures = run.error_details["source_failures"]
    assert failures["S13"]["retryable"] is False
    assert failures["S14"]["retryable"] is True

    persisted_run, artifacts, item, batch = await _state(scenario)
    assert persisted_run.pipeline_generation == 0
    assert item is not None and item.auto_recovery_count == 0
    assert batch is not None and batch.status is ProductionBatchStatus.COMPLETED_WITH_ISSUES
    assert ProductionRecoveryPolicyV1.disposition_for_run(run) is (
        ProductionRecoveryPolicyV1.MANUAL_ONLY
    )
    await _assert_artifact_invariants(scenario, artifacts)
    jobs = await _jobs_for_run(scenario)
    assert len(jobs) == 3
    assert not any(call.stage == "synthesis" for call in scenario.model.calls)


@pytest.mark.asyncio
async def test_operator_retry_is_distinct_and_stales_only_downstream_artifacts(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, _ = _configured(production_scenario_factory, count=2)
    await scenario.start()
    first = await scenario.run_until_terminal()
    assert first.status is SubjectProductionStatus.READY
    _, initial_artifacts, item, _ = await _state(scenario)
    assert item is not None and item.auto_recovery_count == 0
    initial_versions = {artifact.stage: artifact.version for artifact in initial_artifacts}
    initial_q2_ids = {
        call.source_url: call.model_run_id
        for call in scenario.model.calls
        if call.stage == "extraction"
    }

    service = SubjectProductionService(scenario.uow_factory)
    await service.mark_failed(
        first.id,
        "operator_review_required",
        "The operator requested a deliberate recomputation.",
    )
    retry = await service.retry_from_stage(first.id, SubjectProductionStage.EXTRACTION)
    assert retry.previous_status is SubjectProductionStatus.FAILED
    assert retry.old_generation == 0
    assert retry.run.pipeline_generation == 1
    assert retry.staled_artifacts == ["extraction", "synthesis", "publication"]

    _, stale_artifacts, _, _ = await _state(scenario)
    assert {
        artifact.stage
        for artifact in stale_artifacts
        if artifact.status is ProductionArtifactStatus.STALE
    } == {
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.PUBLICATION,
    }
    assert next(
        artifact
        for artifact in stale_artifacts
        if artifact.stage is ProductionArtifactStage.REFERENCES
    ).status is ProductionArtifactStatus.VERIFIED

    await _dispatch_stage(scenario, retry.run, SubjectProductionStage.EXTRACTION)
    await scenario.runner.run_until_idle()
    final, artifacts, item, batch = await _state(scenario)
    assert final.status is SubjectProductionStatus.READY
    assert final.pipeline_generation == 1
    assert item is not None and item.auto_recovery_count == 0
    assert batch is not None and batch.status is ProductionBatchStatus.COMPLETED
    await _assert_artifact_invariants(scenario, artifacts)
    assert {
        artifact.stage: artifact.version
        for artifact in artifacts
        if artifact.status is not ProductionArtifactStatus.STALE
    } == {
        ProductionArtifactStage.REFERENCES: initial_versions[ProductionArtifactStage.REFERENCES],
        ProductionArtifactStage.EXTRACTION: (
            initial_versions[ProductionArtifactStage.EXTRACTION] + 1
        ),
        ProductionArtifactStage.SYNTHESIS: initial_versions[ProductionArtifactStage.SYNTHESIS] + 1,
        ProductionArtifactStage.PUBLICATION: (
            initial_versions[ProductionArtifactStage.PUBLICATION] + 1
        ),
    }
    q2_ids = {
        call.source_url: call.model_run_id
        for call in scenario.model.calls
        if call.stage == "extraction"
    }
    assert set(q2_ids) == set(initial_q2_ids)
    assert all(q2_ids[url] == initial_q2_ids[url] for url in q2_ids)


@pytest.mark.asyncio
async def test_concurrent_start_requests_create_one_run_and_one_job(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, _ = _configured(production_scenario_factory, count=2)
    await scenario.seed()
    results = await asyncio.gather(
        *(
            _create_and_start_run(
                scenario.uow_factory,
                scenario.jobs,
                scenario.runner,
                subject_id=scenario.subject.id,
                edition_id=scenario.edition.id,
                actor_id="operator-test",
            )
            for _ in range(2)
        )
    )
    scenario.run_id = results[0][0].id

    assert {result[0].id for result in results} == {scenario.run_id}
    job_ids = {job_id for _, job_id in results if job_id is not None}
    assert len(job_ids) == 1
    async with scenario.uow_factory() as uow:
        runs = await uow.subject_production_runs.list_for_edition(scenario.edition.id)
    assert len(runs) == 1
    jobs = await _jobs_for_run(scenario)
    assert len(jobs) == 1
    assert jobs[0].id in job_ids
    assert jobs[0].idempotency_key == production_stage_idempotency_key(
        runs[0], SubjectProductionStage.SOURCES
    )

    await scenario.runner.run_until_idle()
    final, artifacts, _, _ = await _state(scenario)
    assert final.status is SubjectProductionStatus.READY
    assert final.pipeline_generation == 0
    assert len(await _jobs_for_run(scenario)) == 5
    await _assert_artifact_invariants(scenario, artifacts)


@pytest.mark.asyncio
async def test_duplicate_job_delivery_has_one_business_effect(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, _ = _configured(production_scenario_factory, count=1)
    await scenario.start()
    before = await scenario.run_until_terminal()
    before_artifacts = await _state(scenario)
    before_model_calls = list(scenario.model.calls)
    extraction_job = next(
        job
        for job in await _jobs_for_run(scenario)
        if job.kind == stage_job_kind(SubjectProductionStage.EXTRACTION)
    )

    await scenario.runner.dispatch(extraction_job.id)
    await scenario.runner.dispatch(extraction_job.id)
    await scenario.runner.run_until_idle()

    after, artifacts, _, _ = await _state(scenario)
    assert after.status is SubjectProductionStatus.READY
    assert after.pipeline_generation == before.pipeline_generation
    assert [(artifact.id, artifact.version, artifact.status) for artifact in artifacts] == [
        (artifact.id, artifact.version, artifact.status)
        for artifact in before_artifacts[1]
    ]
    assert scenario.model.calls == before_model_calls
    jobs = await _jobs_for_run(scenario)
    assert len([job for job in jobs if job.kind == extraction_job.kind]) == 1
    assert extraction_job.status is JobStatus.SUCCEEDED
    assert len({call.model_run_id for call in scenario.model.calls if call.model_run_id}) == 3
    await _assert_artifact_invariants(scenario, artifacts)


@pytest.mark.asyncio
async def test_crash_after_durable_model_response_replays_without_resubmission(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, _ = _configured(production_scenario_factory, count=1)
    original_execute = scenario.model.execute
    failpoint_open = True

    async def crash_after_response(request: Any, role: Any) -> Any:
        nonlocal failpoint_open
        result = await original_execute(request, role)
        if request.prompt_template_id == "production-q2-url" and failpoint_open:
            failpoint_open = False
            raise BridgeTransportError(
                "bridge_unreachable",
                "test failpoint after durable ModelRun response",
                retryable=True,
                phase="pre_submission",
                submission_state="pre_submission",
            )
        return result

    with patch.object(scenario.model, "execute", side_effect=crash_after_response):
        await scenario.start()
        run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.READY
    q2_calls = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert len(q2_calls) == 2
    assert q2_calls[0].model_run_id == q2_calls[1].model_run_id
    assert q2_calls[0].model_run_id is not None
    async with scenario.uow_factory() as uow:
        model_run = await uow.model_runs.get(q2_calls[0].model_run_id)
    assert model_run is not None
    assert model_run.status is ModelRunStatus.SUCCEEDED
    assert model_run.submission_attempt == 1
    assert model_run.parameters["q2_checkpoint_keys"]
    _, artifacts, _, _ = await _state(scenario)
    assert (
        len(
            [
                artifact
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.EXTRACTION
            ]
        )
        == 1
    )
    await _assert_artifact_invariants(scenario, artifacts)


class _VisibleRecoveryBridge:
    def __init__(self, text: str) -> None:
        self.text = text
        self.previews = 0
        self.releases = 0

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]:
        self.previews += 1
        return {
            "bridge_run_id": bridge_run_id,
            "turn_id": "stable-external-turn-8",
            "text": self.text,
            "metadata": {"turn_id": "stable-external-turn-8"},
        }

    async def release_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]:
        assert bridge_run_id == "bridge-post-submission-8"
        self.releases += 1
        return {"released": True}


@pytest.mark.asyncio
async def test_post_submission_ambiguity_reconciles_exact_model_run_without_resubmit(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, urls = _configured(production_scenario_factory, count=1)
    # Keep the adopted bytes distinct from any plain-text model output already
    # present in the content-addressed catalog while preserving Q2 semantics.
    q2_response = f"{_q2_response(1)}\n"
    scenario.model.script.q2(
        source_url=urls[0],
        access_mode="live_url",
        response=BridgeTransportError(
            "bridge_timeout",
            "the provider may already have received the prompt",
            retryable=True,
            phase="generation",
            submission_state="post_submission",
            bridge_run_id="bridge-post-submission-8",
        ),
    )
    bridge = _VisibleRecoveryBridge(q2_response)

    await scenario.start()
    review = await scenario.run_until_terminal()
    assert review.status is SubjectProductionStatus.NEEDS_REVIEW
    assert review.reconciliation is not None
    original_model_run_id = review.reconciliation.model_run_id
    assert review.reconciliation.bridge_response_id == "bridge-post-submission-8"

    reconciliation = ProductionReconciliationService(
        scenario.uow_factory,
        scenario.model,
        scenario.jobs,
        scenario.runner,
        bridge,
    )
    preview = await reconciliation.preview_visible(review.id)
    adopted = await reconciliation.adopt_visible(
        review.id, preview.sha256, actor_id="operator-test"
    )
    await scenario.runner.run_until_idle()

    final, artifacts, item, batch = await _state(scenario)
    assert final.status is SubjectProductionStatus.READY
    assert final.error_code is None
    assert final.reconciliation is not None
    assert final.reconciliation.model_run_id == original_model_run_id
    assert final.reconciliation.output_sha256 == hashlib.sha256(q2_response.encode()).hexdigest()
    assert final.reconciliation.provenance == "visible_recovery"
    assert adopted["model_run_id"] == str(original_model_run_id)
    assert bridge.releases == 1
    assert item is not None and item.auto_recovery_count == 0
    assert batch is not None and batch.status is ProductionBatchStatus.COMPLETED
    async with scenario.uow_factory() as uow:
        model_run = await uow.model_runs.get(original_model_run_id)
        resume_jobs = [
            job
            for job in await _jobs_for_run(scenario)
            if job.kind == "production.subject.reconciliation_resume"
        ]
    assert model_run is not None
    assert model_run.status is ModelRunStatus.SUCCEEDED
    assert model_run.submission_state is ModelSubmissionState.SUBMITTED_OR_UNKNOWN
    assert model_run.submission_attempt == 1
    assert len(resume_jobs) == 1
    assert (
        len({call.model_run_id for call in scenario.model.calls if call.stage == "extraction"})
        == 1
    )
    await _assert_artifact_invariants(scenario, artifacts)


@pytest.mark.asyncio
async def test_non_blocking_skipped_source_does_not_trigger_recovery(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, urls = _configured(production_scenario_factory, count=1)
    scenario.sources[urls[0]]["body"] = ""
    scenario.model.script.q2(
        source_url=urls[0],
        access_mode="live_url",
        response="UNAVAILABLE",
    )

    await scenario.start()
    run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.READY
    assert run.error_code is None
    assert run.pipeline_generation == 0
    persisted_run, artifacts, item, batch = await _state(scenario)
    assert persisted_run.extraction_progress is not None
    source_progress = persisted_run.extraction_progress["sources"]
    assert source_progress[0]["status"] == "skipped"
    assert item is not None and item.auto_recovery_count == 0
    assert batch is not None and batch.status is ProductionBatchStatus.COMPLETED
    extraction = next(
        artifact for artifact in artifacts if artifact.stage is ProductionArtifactStage.EXTRACTION
    )
    diagnostics = extraction.metadata["deterministic_verification"]
    assert diagnostics["source_skips"]["S1"]["blocking"] is False
    assert diagnostics["failed_source_ids"] == []
    assert len([call for call in scenario.model.calls if call.stage == "extraction"]) == 1
    assert not any(call.stage == "extraction" for call in scenario.model.calls[3:])
    await _assert_artifact_invariants(scenario, artifacts)


@pytest.mark.asyncio
async def test_cleanup_failure_after_success_keeps_verified_artifact_and_progresses(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, _ = _configured(production_scenario_factory, count=1)

    async def fail_cleanup(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("test browser close failure")

    with patch.object(scenario.model_service, "archive", side_effect=fail_cleanup):
        await scenario.start()
        run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.READY
    persisted_run, artifacts, item, batch = await _state(scenario)
    assert persisted_run.status is SubjectProductionStatus.READY
    assert item is not None and item.auto_recovery_count == 0
    assert batch is not None and batch.status is ProductionBatchStatus.COMPLETED
    assert all(artifact.status is ProductionArtifactStatus.VERIFIED for artifact in artifacts)
    assert len([call for call in scenario.model.calls if call.stage == "references"]) == 1
    assert len([call for call in scenario.model.calls if call.stage == "synthesis"]) == 1
    assert len(await _jobs_for_run(scenario)) == 5
    cleanup_events = [
        event
        for event in _diagnostic_events(scenario)
        if event.get("event") == "production.conversation_close_failed"
    ]
    assert {event["stage"] for event in cleanup_events} == {"references", "synthesis"}
    await _assert_artifact_invariants(scenario, artifacts)

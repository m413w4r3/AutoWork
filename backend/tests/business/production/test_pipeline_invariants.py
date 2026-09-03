"""Property-style business invariants for the Production pipeline.

Hypothesis is intentionally not used here: it is not a project dependency and
the useful state space is small enough to cover with deterministic, targeted
scenario matrices.  The scenario fixture keeps PostgreSQL, the blob catalog,
the real workflow, jobs and repositories in the loop; only the HTTP/model
boundaries are scripted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from itertools import count
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from cti_app.api.production import _create_and_start_run
from cti_app.application.model_gateway import (
    ModelGatewayError,
    ModelRequest,
    ModelRole,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
from cti_app.application.production_jobs import (
    ProductionStageChain,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.application.production_workflow import (
    _classify_q2_failure,
    _is_q2_source_unavailable,
    _q2_archive_fallback_checkpoint_key,
    _q2_checkpoint_key,
)
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.model_runs import ModelProvider, ModelRunStatus
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ExtractionProfile,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionReconciliationRequiredError,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.integrations.models import BridgeTransportError
from tests.integration.production.support import ProductionScenario

pytest_plugins = (
    "tests.integration.conftest",
    "tests.integration.production.conftest",
)
pytestmark = pytest.mark.integration

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]
_EDITION_CODES = count()


def _urls(source_count: int) -> tuple[str, ...]:
    return tuple(f"https://invariants.test/source-{index}" for index in range(1, source_count + 1))


def _source_specs(
    urls: tuple[str, ...], *, empty_urls: frozenset[str] = frozenset()
) -> dict[str, dict[str, object]]:
    return {
        url: {
            "status": 200,
            "mime": "text/plain",
            "body": (
                ""
                if url in empty_urls
                else f"ExampleRAT source {index} source-{index}.security-lab.io was archived."
            ),
        }
        for index, url in enumerate(urls, start=1)
    }


def _references(urls: tuple[str, ...]) -> str:
    lines = ["# REFERENCES", "editorial-title: [Publication] invariant coverage", ""]
    for index, url in enumerate(urls, start=1):
        lines.extend(
            (
                f"## SOURCE S{index}",
                f"title: ExampleRAT invariant source {index}",
                f"url: {url}",
                f"publisher: Invariant Lab {index}",
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
            "text: The selected reports document the same ExampleRAT activity.",
        )
    )
    return "\n".join(lines)


def _q2(index: int) -> str:
    return (
        "FACT malware\n"
        "- ExampleRAT :: The report documents the malware family.\n\n"
        "IOC confirmed domain\n"
        f"- source-{index}.security-lab.io :: Infrastructure observed in source {index}."
    )


def _synthesis(urls: tuple[str, ...]) -> str:
    citations = " ".join(f"[S{index}]" for index in range(1, len(urls) + 1))
    return f"ExampleRAT activity is documented by the selected reports {citations}."


def _edition_code() -> str:
    value = next(_EDITION_CODES)
    return f"{chr(65 + value % 26)}{chr(65 + (value // 26) % 26)}"


def _configure(
    factory: ScenarioFactory,
    source_count: int,
    *,
    all_core: bool = True,
    empty_urls: frozenset[str] = frozenset(),
    live_q2: Mapping[str, str | Exception] | None = None,
    fallback_q2: Mapping[str, str | Exception] | None = None,
) -> tuple[ProductionScenario, tuple[str, ...]]:
    urls = _urls(source_count)
    scenario = factory(_source_specs(urls, empty_urls=empty_urls))
    scenario.edition.country_code = _edition_code()
    scenario.edition.country = "Production Invariant Tests"
    if all_core:
        scenario.restrict_core_sources(urls)
    scenario.model.script.references(_references(urls))
    scenario.model.script.synthesis(_synthesis(urls))
    for index, url in enumerate(urls, start=1):
        scenario.model.script.q2(
            source_url=url,
            access_mode="live_url",
            response=(live_q2 or {}).get(url, _q2(index)),
        )
        if fallback_q2 is not None and url in fallback_q2:
            scenario.model.script.q2(
                source_url=url,
                access_mode="archive_fallback",
                response=fallback_q2[url],
            )
    return scenario, urls


async def _state(scenario: ProductionScenario) -> tuple[Any, list[Any], Any, Any]:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        run = await uow.subject_production_runs.get(scenario.run_id)
        artifacts = list(await uow.production_artifacts.list_for_run(scenario.run_id))
        item = await uow.edition_production_batch_items.get_by_run(scenario.run_id)
        batch = await uow.edition_production_batches.get(item.batch_id) if item else None
    assert run is not None
    return run, artifacts, item, batch


def _artifact_projection(artifacts: list[Any]) -> tuple[tuple[str, int, str, str], ...]:
    return tuple(
        sorted(
            (
                artifact.stage.value,
                artifact.version,
                artifact.status.value,
                artifact.input_hash,
            )
            for artifact in artifacts
        )
    )


def _q2_calls(scenario: ProductionScenario) -> list[Any]:
    return [call for call in scenario.model.calls if call.stage == "extraction"]


def _provider_q2_calls(scenario: ProductionScenario) -> list[Any]:
    return [
        request
        for request in scenario.model.provider_calls
        if request.prompt_template_id.startswith("production-q2")
    ]


def _progress_projection(run: Any) -> tuple[tuple[str, str], ...]:
    progress = run.extraction_progress or {}
    return tuple(
        sorted(
            (str(item["source_id"]), str(item["status"])) for item in progress.get("sources", [])
        )
    )


def _assert_one_active_verified_per_stage(artifacts: list[Any]) -> None:
    for stage in ProductionArtifactStage:
        active = [
            artifact
            for artifact in artifacts
            if artifact.stage is stage and artifact.status is not ProductionArtifactStatus.STALE
        ]
        assert len(active) <= 1
        assert all(artifact.status is ProductionArtifactStatus.VERIFIED for artifact in active)


def _assert_terminal_source_categories(run: Any, expected_ids: set[str]) -> None:
    progress = run.extraction_progress
    assert progress is not None
    entries = progress["sources"]
    assert len(entries) == len(expected_ids)
    statuses = {"succeeded", "cached", "skipped", "failed", "needs_review"}
    observed_ids = [str(entry["source_id"]) for entry in entries]
    assert set(observed_ids) == expected_ids
    assert len(observed_ids) == len(set(observed_ids))
    assert all(entry["status"] in statuses for entry in entries)
    assert all(entry["status"] not in {"pending", "running"} for entry in entries)


@pytest.mark.asyncio
async def test_pipeline_advances_only_after_verified_upstream_stage(
    production_scenario_factory: ScenarioFactory,
) -> None:
    """Every hand-off has a durable verified artifact for the stage just run."""
    scenario, _ = _configure(production_scenario_factory, 2)
    await scenario.start()

    expected = (
        (SubjectProductionStage.REFERENCES, None),
        (SubjectProductionStage.EXTRACTION, ProductionArtifactStage.REFERENCES),
        (SubjectProductionStage.SYNTHESIS, ProductionArtifactStage.EXTRACTION),
        (SubjectProductionStage.ASSEMBLY, ProductionArtifactStage.SYNTHESIS),
        (SubjectProductionStage.ASSEMBLY, ProductionArtifactStage.PUBLICATION),
    )
    for next_stage, artifact_stage in expected:
        assert await scenario.runner.run_next()
        run, artifacts, _, _ = await _state(scenario)
        assert run.current_stage is next_stage
        if artifact_stage is not None:
            current = [artifact for artifact in artifacts if artifact.stage is artifact_stage]
            assert len(current) == 1
            assert current[0].status is ProductionArtifactStatus.VERIFIED
        for later in ProductionArtifactStage:
            if (
                artifact_stage is not None
                and later is not artifact_stage
                and (
                    [item for item in artifacts if item.stage is later]
                    and list(ProductionArtifactStage).index(later)
                    > list(ProductionArtifactStage).index(artifact_stage)
                )
            ):
                raise AssertionError(f"downstream artifact appeared before {artifact_stage.value}")

    run, artifacts, _, _ = await _state(scenario)
    assert run.status is SubjectProductionStatus.READY
    assert artifacts
    _assert_one_active_verified_per_stage(artifacts)


@pytest.mark.asyncio
async def test_ready_requires_verified_publication_and_all_upstream_artifacts(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, urls = _configure(production_scenario_factory, 2)
    await scenario.start()
    run = await scenario.run_until_terminal()
    persisted, artifacts, _, _ = await _state(scenario)

    assert run.status is SubjectProductionStatus.READY
    assert persisted.status is SubjectProductionStatus.READY
    by_stage = {artifact.stage: artifact for artifact in artifacts}
    assert {
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.PUBLICATION,
    } <= set(by_stage)
    assert all(
        artifact.status is ProductionArtifactStatus.VERIFIED for artifact in by_stage.values()
    )
    assert persisted.reconciliation is None
    assert persisted.error_code is None
    assert not (persisted.error_details or {}).get("source_failures")
    _assert_terminal_source_categories(
        persisted, {f"S{index}" for index in range(1, len(urls) + 1)}
    )
    _assert_one_active_verified_per_stage(artifacts)


@pytest.mark.parametrize("retryable", [False, None, True])
def test_blocking_failure_controls_auto_recovery(retryable: bool | None) -> None:
    """A single non-affirmative blocking failure dominates aggregate recovery."""
    run = SubjectProductionRun(
        subject_id=UUID("00000000-0000-0000-0000-000000000001"),
        edition_id=UUID("00000000-0000-0000-0000-000000000002"),
        status=SubjectProductionStatus.NEEDS_REVIEW,
        current_stage=SubjectProductionStage.EXTRACTION,
        error_code=ProductionRecoveryPolicyV1.Q2_SOURCE_COVERAGE_ERROR_CODE,
        error_details={
            "source_failures": {
                "S1": {
                    "error_code": "source_failure",
                    "retryable": retryable,
                    "contributes_to_coverage": True,
                },
                "S2": {
                    "error_code": "bridge_unreachable",
                    "retryable": True,
                    "contributes_to_coverage": True,
                },
            }
        },
    )
    disposition = ProductionRecoveryPolicyV1.disposition_for_run(run)
    assert disposition is (
        ProductionRecoveryPolicyV1.AUTO
        if retryable is True
        else ProductionRecoveryPolicyV1.MANUAL_ONLY
    )
    assert ProductionRecoveryPolicyV1.current_stage_retry_recommended(run) is (retryable is True)


@pytest.mark.parametrize(
    ("source_count", "skipped_count"),
    [
        (1, 0),
        (1, 1),
        (2, 1),
        (2, 2),
        (4, 3),
        (4, 4),
        (8, 7),
        (8, 8),
    ],
)
@pytest.mark.asyncio
async def test_source_skips_are_local_and_do_not_create_global_q2_failure(
    production_scenario_factory: ScenarioFactory,
    source_count: int,
    skipped_count: int,
) -> None:
    urls = _urls(source_count)
    empty_urls = frozenset(urls[-skipped_count:]) if skipped_count else frozenset()
    scenario, _ = _configure(
        production_scenario_factory,
        source_count,
        empty_urls=empty_urls,
        live_q2={url: "UNAVAILABLE" for url in empty_urls},
    )
    await scenario.start()
    run = await scenario.run_until_terminal()
    persisted, artifacts, _, _ = await _state(scenario)

    assert run.status is SubjectProductionStatus.READY
    assert persisted.current_stage is SubjectProductionStage.ASSEMBLY
    assert persisted.error_code is None
    assert "q2_source_coverage_failed" not in str(persisted.error_details)
    progress = persisted.extraction_progress
    assert progress is not None
    assert progress["skipped_sources"] == skipped_count
    assert progress["completed_sources"] == source_count - skipped_count
    assert {
        entry["source_id"] for entry in progress["sources"] if entry["status"] == "skipped"
    } == {f"S{index}" for index in range(source_count - skipped_count + 1, source_count + 1)}
    _assert_terminal_source_categories(
        persisted, {f"S{index}" for index in range(1, source_count + 1)}
    )
    assert any(artifact.stage is ProductionArtifactStage.EXTRACTION for artifact in artifacts)
    assert not (persisted.error_details or {}).get("source_failures")


@pytest.mark.asyncio
async def test_reconciliation_is_exclusive_until_explicit_adoption(
    production_scenario_factory: ScenarioFactory,
) -> None:
    url = _urls(1)[0]
    reconciliation_error = BridgeTransportError(
        "bridge_timeout",
        "provider may already have received the prompt",
        retryable=True,
        phase="generation",
        submission_state="post_submission",
        bridge_run_id="bridge-invariant-reconciliation",
    )
    scenario, urls = _configure(
        production_scenario_factory,
        1,
        live_q2={url: reconciliation_error},
        fallback_q2={url: _q2(1)},
    )
    await scenario.start()
    review = await scenario.run_until_terminal()
    before_calls = len(_q2_calls(scenario))
    _, artifacts, item, batch = await _state(scenario)

    assert review.status is SubjectProductionStatus.NEEDS_REVIEW
    assert review.current_stage is SubjectProductionStage.EXTRACTION
    assert review.error_code == PRODUCTION_RECONCILIATION_ERROR_CODE
    assert review.requires_reconciliation
    assert item is not None and item.auto_recovery_count == 0
    assert batch is not None and batch.phase.value == "review"
    assert not any(
        call.request.metadata.get("access_mode") == "archive_fallback"
        for call in _q2_calls(scenario)
    )
    assert not any(call.stage == "synthesis" for call in scenario.model.calls)
    assert not any(artifact.stage is ProductionArtifactStage.EXTRACTION for artifact in artifacts)
    assert ProductionRecoveryPolicyV1.disposition_for_run(review) is (
        ProductionRecoveryPolicyV1.MANUAL_ONLY
    )

    service = SubjectProductionService(scenario.uow_factory)
    with pytest.raises(ProductionReconciliationRequiredError):
        await service.retry_from_stage(review.id, SubjectProductionStage.EXTRACTION)
    chain = ProductionStageChain()
    chain.bind(scenario.jobs, scenario.runner)
    with pytest.raises(ProductionReconciliationRequiredError):
        await chain.submit(
            run=review,
            stage=SubjectProductionStage.EXTRACTION,
            correlation_id="invariant-test",
        )
    assert len(_q2_calls(scenario)) == before_calls
    assert set(urls) == {url}


@pytest.mark.parametrize("archive_present", [False, True])
@pytest.mark.parametrize("failure_kind", ["infrastructure", "reconciliation"])
@pytest.mark.asyncio
async def test_infrastructure_and_reconciliation_failures_never_use_archive_fallback(
    production_scenario_factory: ScenarioFactory,
    archive_present: bool,
    failure_kind: str,
) -> None:
    url = _urls(1)[0]
    failure: Exception
    if failure_kind == "infrastructure":
        failure = BridgeTransportError(
            "bridge_timeout",
            "bridge unavailable before provider submission",
            retryable=True,
            phase="pre_submission",
            submission_state="pre_submission",
        )
    else:
        failure = BridgeTransportError(
            "bridge_timeout",
            "provider submission is ambiguous",
            retryable=True,
            phase="generation",
            submission_state="post_submission",
            bridge_run_id="bridge-invariant-no-fallback",
        )
    scenario, _ = _configure(
        production_scenario_factory,
        1,
        empty_urls=frozenset() if archive_present else frozenset({url}),
        live_q2={url: failure},
        fallback_q2={url: _q2(1)},
    )
    await scenario.start()
    await scenario.run_until_terminal()

    q2_calls = _q2_calls(scenario)
    assert q2_calls
    assert all(call.request.metadata.get("access_mode") != "archive_fallback" for call in q2_calls)
    assert all(
        call.request.prompt_template_id != "production-q2-url-archive-fallback" for call in q2_calls
    )
    if failure_kind == "reconciliation":
        assert len(q2_calls) == 1


@pytest.mark.asyncio
async def test_archive_fallback_requires_one_prior_live_unavailable_attempt(
    production_scenario_factory: ScenarioFactory,
) -> None:
    url = _urls(1)[0]
    scenario, _ = _configure(
        production_scenario_factory,
        1,
        live_q2={url: "UNAVAILABLE"},
        fallback_q2={url: _q2(1)},
    )
    await scenario.start()
    run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.READY
    q2_calls = _q2_calls(scenario)
    assert len(q2_calls) == 2
    live_call, fallback_call = q2_calls
    assert live_call.source_url == fallback_call.source_url == url
    assert live_call.request.prompt_template_id == "production-q2-url"
    assert live_call.request.web_search is True
    assert live_call.request.metadata.get("access_mode") is None
    assert fallback_call.request.metadata.get("access_mode") == "archive_fallback"
    events_path = scenario.blob_root.parent / "diagnostics" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    fallback_events = [
        event for event in events if event.get("event") == "q2.source.archive_fallback_completed"
    ]
    assert len(fallback_events) == 1
    assert fallback_events[0]["live_failure_code"] == "q2_source_unavailable"


@pytest.mark.parametrize(
    "failure",
    [
        BridgeTransportError(
            "bridge_timeout",
            "pre-submission transport failure",
            retryable=True,
            phase="pre_submission",
            submission_state="pre_submission",
        ),
        ModelSubmissionReconciliationRequiredError(),
        ModelGatewayError("failed ModelRun cannot be resubmitted"),
    ],
)
def test_q2_failure_classification_keeps_infra_reconciliation_and_control_distinct(
    failure: Exception,
) -> None:
    classification = _classify_q2_failure(failure)
    assert classification.failure_class.value in {
        "global_transient_pre_submission",
        "reconciliation_required",
        "control_invariant_failure",
    }
    assert not classification.contributes_to_coverage
    assert not _is_q2_source_unavailable((classification.error_code,))


@pytest.mark.asyncio
async def test_q2_batch_outputs_keep_exact_url_batch_and_source_identity(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, urls = _configure(production_scenario_factory, 8, all_core=False)
    await scenario.start()
    run = await scenario.run_until_terminal()
    _, artifacts, _, _ = await _state(scenario)
    extraction = next(
        artifact for artifact in artifacts if artifact.stage is ProductionArtifactStage.EXTRACTION
    )
    assert extraction.canonical_blob_id is not None
    payload = await scenario.artifact_store.read_json(extraction.canonical_blob_id)

    expected_values = {
        f"source-{index}.security-lab.io": f"S{index}" for index in range(1, len(urls) + 1)
    }
    observed_values = {
        item["value"]: tuple(item["source_ids"])
        for item in payload["items"]
        if isinstance(item, dict) and item.get("value") in expected_values
    }
    assert {
        value: source_ids[0] for value, source_ids in observed_values.items()
    } == expected_values
    assert run.extraction_progress is not None
    assert {item["source_id"] for item in run.extraction_progress["sources"]} == {
        f"S{index}" for index in range(1, 9)
    }

    q2_calls = _q2_calls(scenario)
    flattened = [url for call in q2_calls for url in call.source_urls]
    assert len(flattened) == len(set(flattened)) == len(urls)
    assert set(flattened) == set(urls)
    for call in q2_calls:
        request = call.request
        if request.prompt_template_id == "production-q2-ioc-batch":
            mapping = request.parameters["q2_batch_sources"]
            assert [item["canonical_url"] for item in mapping] == list(call.source_urls)
            assert request.metadata["batch_source_urls"] == list(call.source_urls)
        else:
            assert request.metadata["source_url"] in urls

    async with scenario.uow_factory() as uow:
        for call in q2_calls:
            assert call.model_run_id is not None
            model_run = await uow.model_runs.get(call.model_run_id)
            assert model_run is not None and model_run.status is ModelRunStatus.SUCCEEDED
            if call.request.prompt_template_id == "production-q2-ioc-batch":
                assert [
                    item["canonical_url"] for item in model_run.parameters["q2_batch_sources"]
                ] == list(call.source_urls)


def test_checkpoint_identity_is_content_profile_contract_and_access_mode_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000010")
    common = {
        "production_run_id": run_id,
        "canonical_url": "https://invariants.test/source-1",
        "profile": ExtractionProfile.FULL,
        "prompt_version": "q2-prompt",
        "batch_parser_version": None,
        "provider": ModelProvider.OPENAI,
        "requested_model": "invariant-model",
    }
    live = _q2_checkpoint_key(**common)
    assert live == _q2_checkpoint_key(**common)
    monkeypatch.setattr(
        "cti_app.application.production_workflow.Q2_EXTRACTION_CONTRACT_VERSION",
        "next-contract",
    )
    assert _q2_checkpoint_key(**common) != live

    archive = _q2_archive_fallback_checkpoint_key(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url=common["canonical_url"],
        source_content_sha256="a" * 64,
        profile=common["profile"],
        provider=ModelProvider.OPENAI,
        requested_model="invariant-model",
    )
    changed_content = _q2_archive_fallback_checkpoint_key(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url=common["canonical_url"],
        source_content_sha256="b" * 64,
        profile=common["profile"],
        provider=ModelProvider.OPENAI,
        requested_model="invariant-model",
    )
    changed_profile = _q2_archive_fallback_checkpoint_key(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url=common["canonical_url"],
        source_content_sha256="a" * 64,
        profile=ExtractionProfile.IOC_RULES,
        provider=ModelProvider.OPENAI,
        requested_model="invariant-model",
    )
    assert archive != live
    assert changed_content != archive
    assert changed_profile != archive


@pytest.mark.asyncio
async def test_content_addressed_store_and_reuse_reject_incompatible_input(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, _ = _configure(production_scenario_factory, 1)
    await scenario.start()
    run = await scenario.run_until_terminal()
    _, artifacts, _, _ = await _state(scenario)
    extraction = next(
        artifact for artifact in artifacts if artifact.stage is ProductionArtifactStage.EXTRACTION
    )
    same_id, same_hash = await scenario.artifact_store.put_canonical_json(
        {"same": True}, bucket="invariant-content-addressed"
    )
    repeated_id, repeated_hash = await scenario.artifact_store.put_canonical_json(
        {"same": True}, bucket="invariant-content-addressed"
    )
    changed_id, changed_hash = await scenario.artifact_store.put_canonical_json(
        {"same": False}, bucket="invariant-content-addressed"
    )
    assert repeated_id == same_id
    assert repeated_hash == same_hash == hashlib.sha256(b'{"same":true}').hexdigest()
    assert changed_id != same_id
    assert changed_hash != same_hash

    store = ProductionArtifactReuseService(scenario.uow_factory, scenario.artifact_store)
    compatible = await store.find_or_reuse(
        run=run,
        stage=ProductionArtifactStage.EXTRACTION,
        input_hash=extraction.input_hash,
    )
    incompatible = await store.find_or_reuse(
        run=run,
        stage=ProductionArtifactStage.EXTRACTION,
        input_hash="f" * 64,
    )
    assert compatible is not None and compatible.artifact.id == extraction.id
    assert incompatible is None


@pytest.mark.asyncio
async def test_retry_from_stage_stales_downstream_and_keeps_versions_monotonic(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, _ = _configure(production_scenario_factory, 2)
    await scenario.start()
    first = await scenario.run_until_terminal()
    _, before_artifacts, _, _ = await _state(scenario)
    before_versions = {artifact.stage: artifact.version for artifact in before_artifacts}

    service = SubjectProductionService(scenario.uow_factory)
    await service.mark_failed(first.id, "operator_retry", "business retry")
    retry = await service.retry_from_stage(first.id, SubjectProductionStage.EXTRACTION)
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
    assert (
        next(
            artifact
            for artifact in stale_artifacts
            if artifact.stage is ProductionArtifactStage.REFERENCES
        ).status
        is ProductionArtifactStatus.VERIFIED
    )

    chain = ProductionStageChain()
    chain.bind(scenario.jobs, scenario.runner)
    job_id = await chain.submit(
        run=retry.run,
        stage=SubjectProductionStage.EXTRACTION,
        correlation_id="invariant-retry",
    )
    assert job_id is not None
    await scenario.runner.run_until_idle()
    final, artifacts, _, _ = await _state(scenario)
    assert final.status is SubjectProductionStatus.READY
    assert final.pipeline_generation == 1
    _assert_one_active_verified_per_stage(artifacts)
    active = {
        artifact.stage: artifact
        for artifact in artifacts
        if artifact.status is not ProductionArtifactStatus.STALE
    }
    assert (
        active[ProductionArtifactStage.REFERENCES].version
        == before_versions[ProductionArtifactStage.REFERENCES]
    )
    for stage in (
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.PUBLICATION,
    ):
        assert active[stage].version > before_versions[stage]


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.asyncio
async def test_cleanup_outcome_does_not_change_stage_business_status(
    production_scenario_factory: ScenarioFactory,
    cleanup_fails: bool,
) -> None:
    scenario, _ = _configure(production_scenario_factory, 1)

    async def fail_cleanup(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("browser cleanup failed in invariant test")

    archive = patch.object(scenario.model_service, "archive", side_effect=fail_cleanup)
    with archive if cleanup_fails else patch.object(scenario.model_service, "archive"):
        await scenario.start()
        run = await scenario.run_until_terminal()
    _, artifacts, _, _ = await _state(scenario)

    assert run.status is SubjectProductionStatus.READY
    assert run.current_stage is SubjectProductionStage.ASSEMBLY
    assert all(artifact.status is ProductionArtifactStatus.VERIFIED for artifact in artifacts)
    assert {artifact.stage for artifact in artifacts} == set(ProductionArtifactStage)


@asynccontextmanager
async def _fresh_runtime(
    scenario: ProductionScenario,
    postgres_url: str,
) -> AsyncIterator[ProductionScenario]:
    engine = create_postgres_engine(postgres_url)
    session_factory = create_session_factory(engine)

    def fresh_uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    try:
        yield await scenario.restart(fresh_uow_factory)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restart_reconstructs_the_same_business_decision_from_postgres_and_blobs(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    uninterrupted, _ = _configure(production_scenario_factory, 2)
    await uninterrupted.start()
    uninterrupted_final = await uninterrupted.run_until_terminal()
    _, uninterrupted_artifacts, _, _ = await _state(uninterrupted)

    restarted_before, urls = _configure(production_scenario_factory, 2)
    await restarted_before.start()
    assert await restarted_before.runner.run_next()
    assert await restarted_before.runner.run_next()
    async with _fresh_runtime(restarted_before, migrated_postgres_url) as restarted:
        _configure_runtime_after_restart(restarted, urls)
        await restarted.enqueue_persisted_jobs()
        restarted_final = await restarted.run_until_terminal()
        _, restarted_artifacts, _, _ = await _state(restarted)

    assert uninterrupted_final.status is restarted_final.status is SubjectProductionStatus.READY
    assert (
        uninterrupted_final.current_stage
        is restarted_final.current_stage
        is SubjectProductionStage.ASSEMBLY
    )
    assert _artifact_stage_projection(uninterrupted_artifacts) == _artifact_stage_projection(
        restarted_artifacts
    )
    assert _progress_projection(uninterrupted_final) == _progress_projection(restarted_final)
    restarted_q2_provider_calls = _provider_q2_calls(restarted)
    assert len(restarted_q2_provider_calls) == len(urls)
    assert [request.metadata["source_url"] for request in restarted_q2_provider_calls] == list(urls)
    assert not any(
        request.prompt_template_id == "analyst-conversation"
        and request.routing_hint.value == "web_research"
        for request in restarted.model.provider_calls
    )


def _configure_runtime_after_restart(scenario: ProductionScenario, urls: tuple[str, ...]) -> None:
    """Configure only post-restart responses; Q1 must come from durable state."""
    scenario.restrict_core_sources(urls)
    scenario.model.script.synthesis(_synthesis(urls))
    for index, url in enumerate(urls, start=1):
        scenario.model.script.q2(source_url=url, access_mode="live_url", response=_q2(index))


def _artifact_stage_projection(artifacts: list[Any]) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        sorted(
            (artifact.stage.value, artifact.version, artifact.status.value)
            for artifact in artifacts
        )
    )


@pytest.mark.asyncio
async def test_duplicate_posts_deliveries_and_worker_retries_have_one_logical_effect(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, _ = _configure(production_scenario_factory, 1)
    await scenario.seed()
    results = await asyncio.gather(
        *(
            _create_and_start_run(
                scenario.uow_factory,
                scenario.jobs,
                scenario.runner,
                subject_id=scenario.subject.id,
                edition_id=scenario.edition.id,
                actor_id="invariant-test",
            )
            for _ in range(3)
        )
    )
    scenario.run_id = results[0][0].id
    assert {result[0].id for result in results} == {scenario.run_id}
    jobs = await scenario.jobs.list_for_aggregate("subject", scenario.subject.id)
    assert (
        len([job for job in jobs if job.kind == stage_job_kind(SubjectProductionStage.SOURCES)])
        == 1
    )

    await scenario.runner.run_until_idle()
    before_run, before_artifacts, _, _ = await _state(scenario)
    before_calls = list(scenario.model.calls)
    extraction_job = next(
        job
        for job in await scenario.jobs.list_for_aggregate("subject", scenario.subject.id)
        if job.kind == stage_job_kind(SubjectProductionStage.EXTRACTION)
    )
    await scenario.runner.dispatch(extraction_job.id)
    await scenario.runner.dispatch(extraction_job.id)
    await scenario.runner.run_until_idle()
    after_run, after_artifacts, _, _ = await _state(scenario)

    assert after_run.status is before_run.status is SubjectProductionStatus.READY
    assert _artifact_projection(after_artifacts) == _artifact_projection(before_artifacts)
    assert scenario.model.calls == before_calls
    assert (
        production_stage_idempotency_key(before_run, SubjectProductionStage.EXTRACTION)
        == extraction_job.idempotency_key
    )


@pytest.mark.asyncio
async def test_compatible_success_checkpoint_adds_zero_provider_calls_for_source(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario, urls = _configure(production_scenario_factory, 1)
    original_execute = scenario.model.execute
    crashed_after_persist = False

    async def crash_after_durable_response(request: ModelRequest, role: ModelRole) -> Any:
        nonlocal crashed_after_persist
        result = await original_execute(request, role)
        if request.prompt_template_id == "production-q2-url" and not crashed_after_persist:
            crashed_after_persist = True
            raise BridgeTransportError(
                "bridge_unreachable",
                "worker lost after the successful response was persisted",
                retryable=True,
                phase="pre_submission",
                submission_state="pre_submission",
            )
        return result

    with patch.object(scenario.model, "execute", side_effect=crash_after_durable_response):
        await scenario.start()
        run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.READY
    assert crashed_after_persist
    q2_logical_calls = [call for call in _q2_calls(scenario) if call.source_url == urls[0]]
    q2_provider_calls = [
        request
        for request in _provider_q2_calls(scenario)
        if request.metadata.get("source_url") == urls[0]
    ]
    assert len(q2_logical_calls) == 2
    assert q2_logical_calls[0].model_run_id == q2_logical_calls[1].model_run_id
    assert len(q2_provider_calls) == 1


@pytest.mark.asyncio
async def test_no_q1_source_disappears_from_terminal_q2_progress(
    production_scenario_factory: ScenarioFactory,
) -> None:
    urls = _urls(4)
    terminal_url = urls[1]
    scenario, _ = _configure(
        production_scenario_factory,
        4,
        live_q2={terminal_url: "not Q2 markdown"},
    )
    await scenario.start()
    run = await scenario.run_until_terminal()
    persisted, _, _, _ = await _state(scenario)

    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert persisted.current_stage is SubjectProductionStage.EXTRACTION
    _assert_terminal_source_categories(persisted, {f"S{index}" for index in range(1, 5)})
    progress = persisted.extraction_progress
    assert progress is not None
    assert progress["sources"][1]["status"] == "failed"
    assert progress["sources"][1]["source_id"] == "S2"
    assert persisted.error_details is not None
    assert set(persisted.error_details["source_failures"]) == {"S2"}

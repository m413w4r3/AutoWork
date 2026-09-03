"""Production pipeline contracts when the worker process disappears."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from cti_app.application.model_gateway import ModelRequest, ModelRole
from cti_app.application.production_reconciliation import ProductionReconciliationService
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.collection import CollectionState
from cti_app.domain.model_conversations import ConversationStatus
from cti_app.domain.model_runs import ModelRunStatus, ModelSubmissionState
from cti_app.domain.production import (
    ProductionArtifactStage,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.integrations.models import BridgeTransportError

from .support import ProductionScenario, ScriptedModelGateway

pytestmark = pytest.mark.integration

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]


class ProcessCrash(BaseException):
    """A test-only process loss which bypasses business exception handlers."""


@dataclass(frozen=True, slots=True)
class DurableState:
    run: Any
    snapshot: Any
    artifacts: tuple[Any, ...]
    collections: tuple[Any, ...]
    documents: tuple[Any, ...]
    jobs: tuple[Any, ...]
    blobs: dict[UUID, bytes]


@dataclass
class BrowserTarget:
    """External browser state needed only to exercise cleanup retry."""

    present: set[UUID]
    calls: list[UUID]

    async def archive_conversation(self, conversation_id: UUID) -> None:
        self.calls.append(conversation_id)
        self.present.discard(conversation_id)


@dataclass
class VisibleRecovery:
    bridge_run_id: str
    text: str
    previews: int = 0
    releases: int = 0

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]:
        self.previews += 1
        return {
            "bridge_run_id": bridge_run_id,
            "turn_id": "dom-turn-restart-7",
            "text": self.text,
            "metadata": {"turn_id": "dom-turn-restart-7", "target_id": "target-restart"},
        }

    async def release_visible_recovery(self, bridge_run_id: str) -> dict[str, bool]:
        assert bridge_run_id == self.bridge_run_id
        self.releases += 1
        return {"released": True}


def _urls(source_count: int) -> tuple[str, ...]:
    return tuple(
        f"https://example.test/restart-source-{index}" for index in range(1, source_count + 1)
    )


def _source_specs(
    urls: tuple[str, ...],
    *,
    bodies: dict[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    return {
        url: {
            "status": 200,
            "mime": "text/plain",
            "body": (bodies or {}).get(
                url,
                f"ExampleRAT source {index} source-{index}.security-lab.io was archived.",
            ),
        }
        for index, url in enumerate(urls, start=1)
    }


def _references(urls: tuple[str, ...]) -> str:
    lines = ["# REFERENCES", "editorial-title: [Publication] Restart safety", ""]
    for index, url in enumerate(urls, start=1):
        lines.extend(
            (
                f"## SOURCE S{index}",
                f"title: ExampleRAT restart source {index}",
                f"url: {url}",
                f"publisher: Restart Lab {index}",
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
            "text: ExampleRAT activity is documented by the restart safety reports.",
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


def _synthesis(urls: tuple[str, ...]) -> str:
    citations = " ".join(f"[S{index}]" for index in range(1, len(urls) + 1))
    return f"ExampleRAT activity is documented by the restart safety reports {citations}."


def _configure_gateway(
    scenario: ProductionScenario,
    urls: tuple[str, ...],
    *,
    references: bool = True,
    synthesis: bool = True,
    live_q2: dict[str, str | Exception] | None = None,
    fallback_q2: dict[str, str | Exception] | None = None,
) -> None:
    if references:
        scenario.model.script.references(_references(urls))
    if synthesis:
        scenario.model.script.synthesis(_synthesis(urls))
    for index, url in enumerate(urls, start=1):
        scenario.model.script.q2(
            source_url=url,
            access_mode="live_url",
            response=(live_q2 or {}).get(url, _q2(index)),
        )
        if fallback_q2 and url in fallback_q2:
            scenario.model.script.q2(
                source_url=url,
                access_mode="archive_fallback",
                response=fallback_q2[url],
            )


def _configured(
    factory: ScenarioFactory,
    source_count: int,
    *,
    all_core: bool = False,
    bodies: dict[str, str] | None = None,
) -> tuple[ProductionScenario, tuple[str, ...]]:
    urls = _urls(source_count)
    scenario = factory(_source_specs(urls, bodies=bodies))
    scenario.edition.country = f"Restart Safety {scenario.edition.country_code}"
    if all_core:
        scenario.restrict_core_sources(urls)
    _configure_gateway(scenario, urls)
    return scenario, urls


@asynccontextmanager
async def _fresh_runtime(
    scenario: ProductionScenario,
    postgres_url: str,
) -> AsyncIterator[ProductionScenario]:
    """Build a new SQLAlchemy UoW factory and a new complete runtime."""
    engine = create_postgres_engine(postgres_url)
    session_factory = create_session_factory(engine)

    def fresh_uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    try:
        restarted = await scenario.restart(fresh_uow_factory)
        yield restarted
    finally:
        await engine.dispose()


async def _reload(scenario: ProductionScenario) -> DurableState:
    assert scenario.run_id is not None
    async with scenario.uow_factory() as uow:
        run = await uow.subject_production_runs.get(scenario.run_id)
        snapshot = await uow.production_input_snapshots.get_by_run(scenario.run_id)
        artifacts = tuple(await uow.production_artifacts.list_for_run(scenario.run_id))
        collections = tuple(await uow.source_collections.list_for_subject(scenario.subject.id))
        documents = tuple(await uow.source_documents.list_for_subject(scenario.subject.id))
        jobs = tuple(await uow.jobs.list_for_aggregate("subject", scenario.subject.id))

    assert run is not None
    assert snapshot is not None
    blobs: dict[UUID, bytes] = {}
    for artifact in artifacts:
        for field in ("raw_blob_id", "canonical_blob_id", "rendered_blob_id"):
            blob_id = getattr(artifact, field)
            if blob_id is not None:
                blobs[blob_id] = await scenario.artifact_store.read_bytes(blob_id)
    for document in documents:
        for blob_id in (document.blob_id, document.decoded_blob_id):
            if blob_id is not None:
                blobs[blob_id] = await scenario.artifact_store.read_bytes(blob_id)
    return DurableState(run, snapshot, artifacts, collections, documents, jobs, blobs)


def _assert_refetched(before: DurableState, after: DurableState) -> None:
    assert after.run is not before.run
    assert after.snapshot is not before.snapshot
    before_artifacts = {artifact.id: artifact for artifact in before.artifacts}
    for artifact in after.artifacts:
        prior = before_artifacts.get(artifact.id)
        if prior is not None:
            assert artifact is not prior
    for blob_id, content in before.blobs.items():
        if blob_id in after.blobs:
            assert after.blobs[blob_id] == content
    for job in after.jobs:
        if job.input_parameters.get("run_id") == str(after.run.id):
            assert job.input_parameters["pipeline_generation"] == after.run.pipeline_generation


def _q2_provider_calls(model: ScriptedModelGateway) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    for request in model.provider_calls:
        if request.prompt_template_id not in {
            "production-q2-url",
            "production-q2-url-archive-fallback",
        }:
            continue
        source_url = request.metadata.get("source_url")
        if not isinstance(source_url, str):
            continue
        access_mode = request.metadata.get("access_mode")
        calls.append((source_url, access_mode if isinstance(access_mode, str) else "live_url"))
    return calls


def _provider_stages(model: ScriptedModelGateway) -> list[str]:
    stages: list[str] = []
    for request in model.provider_calls:
        if request.prompt_template_id.startswith("production-q2"):
            stages.append("extraction")
        elif request.routing_hint.value == "web_research":
            stages.append("references")
        elif request.routing_hint.value == "standard_draft":
            stages.append("synthesis")
    return stages


async def _run_prefix(scenario: ProductionScenario, count: int) -> None:
    for _ in range(count):
        assert await scenario.runner.run_next()


@pytest.mark.asyncio
async def test_restart_after_sources_reconstructs_the_pipeline(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, urls = _configured(production_scenario_factory, 2)
    await scenario.start()
    assert await scenario.runner.run_next()
    before = await _reload(scenario)
    assert before.run.current_stage is SubjectProductionStage.REFERENCES
    assert all(item.state is CollectionState.ARCHIVED for item in before.collections)
    assert not [
        artifact
        for artifact in before.artifacts
        if artifact.stage is ProductionArtifactStage.REFERENCES
    ]

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        _configure_gateway(restarted, urls)
        await restarted.enqueue_persisted_jobs()
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is SubjectProductionStatus.READY
    assert after.run.current_stage is SubjectProductionStage.ASSEMBLY
    assert _provider_stages(restarted.model) == [
        "references",
        "extraction",
        "extraction",
        "synthesis",
    ]
    assert _provider_stages(scenario.model) == []
    assert {artifact.stage for artifact in after.artifacts} == {
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.PUBLICATION,
    }


@pytest.mark.asyncio
async def test_restart_after_references_reads_the_persisted_artifact(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, urls = _configured(production_scenario_factory, 2)
    await scenario.start()
    await _run_prefix(scenario, 2)
    before = await _reload(scenario)
    references = next(
        artifact
        for artifact in before.artifacts
        if artifact.stage is ProductionArtifactStage.REFERENCES
    )
    assert references.canonical_blob_id is not None
    references_payload = before.blobs[references.canonical_blob_id]
    assert before.run.current_stage is SubjectProductionStage.EXTRACTION

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        # Deliberately do not configure a References answer. A call would be
        # an assertion failure in the fake adapter.
        _configure_gateway(restarted, urls, references=False)
        await restarted.enqueue_persisted_jobs()
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is SubjectProductionStatus.READY
    assert _provider_stages(restarted.model) == ["extraction", "extraction", "synthesis"]
    assert _provider_stages(restarted.model).count("references") == 0
    reloaded_references = next(
        artifact
        for artifact in after.artifacts
        if artifact.stage is ProductionArtifactStage.REFERENCES
    )
    assert reloaded_references.canonical_blob_id is not None
    assert after.blobs[reloaded_references.canonical_blob_id] == references_payload


@pytest.mark.asyncio
async def test_restart_mid_q2_reuses_only_the_durable_completed_checkpoints(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, urls = _configured(production_scenario_factory, 3, all_core=True)
    await scenario.start()
    await _run_prefix(scenario, 2)

    original_persist = ProductionWorkflowOrchestrator._persist_extraction_progress
    crashed = False

    async def persist_then_crash(
        orchestrator: ProductionWorkflowOrchestrator,
        run_id: UUID,
        progress: dict[str, Any],
    ) -> None:
        nonlocal crashed
        await original_persist(orchestrator, run_id, progress)
        statuses = {item["source_id"]: item["status"] for item in progress["sources"]}
        if not crashed and statuses.get("S1") == "succeeded" and statuses.get("S2") == "succeeded":
            crashed = True
            raise ProcessCrash("process lost before S3")

    with patch.object(
        ProductionWorkflowOrchestrator,
        "_persist_extraction_progress",
        new=persist_then_crash,
    ):
        with pytest.raises(ProcessCrash):
            await scenario.runner.run_next()

    before = await _reload(scenario)
    assert crashed
    assert before.run.extraction_progress is not None
    assert {
        item["source_id"]: item["status"] for item in before.run.extraction_progress["sources"]
    } == {"S1": "succeeded", "S2": "succeeded", "S3": "pending"}
    q2_before = [call for call in scenario.model.calls if call.stage == "extraction"]
    assert [call.source_url for call in q2_before] == list(urls[:2])

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        _configure_gateway(restarted, urls, references=False)
        await restarted.enqueue_persisted_jobs(recover_abandoned=True)
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is SubjectProductionStatus.READY
    assert _q2_provider_calls(restarted.model) == [(urls[2], "live_url")]
    assert _q2_provider_calls(scenario.model) == [(urls[0], "live_url"), (urls[1], "live_url")]

    extraction = next(
        artifact
        for artifact in after.artifacts
        if artifact.stage is ProductionArtifactStage.EXTRACTION
    )
    assert extraction.canonical_blob_id is not None
    payload = json.loads(after.blobs[extraction.canonical_blob_id])
    values = {
        item["value"]: tuple(item["source_ids"])
        for item in payload["items"]
        if isinstance(item, dict) and "value" in item
    }
    for index in range(1, 4):
        assert values[f"source-{index}.security-lab.io"] == (f"S{index}",)

    async with restarted.uow_factory() as uow:
        for call in q2_before:
            assert call.model_run_id is not None
            checkpoint = await uow.model_runs.get(call.model_run_id)
            assert checkpoint is not None
            assert checkpoint.status is ModelRunStatus.SUCCEEDED
            assert checkpoint.parameters.get("q2_checkpoint_keys")


@pytest.mark.asyncio
async def test_restart_between_live_unavailable_and_archive_fallback(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, urls = _configured(production_scenario_factory, 2, all_core=True)
    _configure_gateway(
        scenario,
        urls,
        live_q2={urls[0]: "UNAVAILABLE", urls[1]: _q2(2)},
        fallback_q2={urls[0]: _q2(1)},
    )
    await scenario.start()
    await _run_prefix(scenario, 2)

    original_execute = scenario.model.execute
    crashed = False

    async def execute_then_crash(request: ModelRequest, role: ModelRole) -> Any:
        nonlocal crashed
        execution = await original_execute(request, role)
        if (
            not crashed
            and request.prompt_template_id == "production-q2-url"
            and request.metadata.get("source_url") == urls[0]
        ):
            crashed = True
            raise ProcessCrash("process lost before archive fallback")
        return execution

    with patch.object(scenario.model, "execute", new=execute_then_crash):
        with pytest.raises(ProcessCrash):
            await scenario.runner.run_next()

    before = await _reload(scenario)
    assert crashed
    assert before.run.extraction_progress is not None
    assert before.run.extraction_progress["sources"][0]["status"] == "running"

    async with scenario.uow_factory() as uow:
        live_model_run_id = next(
            call.model_run_id
            for call in scenario.model.calls
            if call.stage == "extraction" and call.source_url == urls[0]
        )
        assert live_model_run_id is not None
        live_run = await uow.model_runs.get(live_model_run_id)
        assert live_run is not None
        assert live_run.status is ModelRunStatus.SUCCEEDED
        assert live_run.raw_output_reference is not None

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        _configure_gateway(
            restarted,
            urls,
            references=False,
            live_q2={urls[0]: "UNAVAILABLE", urls[1]: _q2(2)},
            fallback_q2={urls[0]: _q2(1)},
        )
        await restarted.enqueue_persisted_jobs(recover_abandoned=True)
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is SubjectProductionStatus.READY
    assert _q2_provider_calls(restarted.model) == [
        (urls[0], "archive_fallback"),
        (urls[1], "live_url"),
    ]
    extraction = next(
        artifact
        for artifact in after.artifacts
        if artifact.stage is ProductionArtifactStage.EXTRACTION
    )
    assert extraction.canonical_blob_id is not None
    payload = json.loads(after.blobs[extraction.canonical_blob_id])
    assert {item["value"] for item in payload["items"]} >= {
        "source-1.security-lab.io",
        "source-2.security-lab.io",
    }


@pytest.mark.asyncio
async def test_restart_after_synthesis_assembly_consumes_the_persisted_artifact(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, _urls = _configured(production_scenario_factory, 1, all_core=True)
    await scenario.start()
    await _run_prefix(scenario, 4)
    before = await _reload(scenario)
    assert before.run.current_stage is SubjectProductionStage.ASSEMBLY
    synthesis = next(
        artifact
        for artifact in before.artifacts
        if artifact.stage is ProductionArtifactStage.SYNTHESIS
    )
    assert synthesis.rendered_blob_id is not None
    synthesis_bytes = before.blobs[synthesis.rendered_blob_id]

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        # Assembly is deterministic and must not need any model answer.
        await restarted.enqueue_persisted_jobs()
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is SubjectProductionStatus.READY
    assert restarted.model.provider_calls == []
    reloaded_synthesis = next(
        artifact
        for artifact in after.artifacts
        if artifact.stage is ProductionArtifactStage.SYNTHESIS
    )
    assert reloaded_synthesis.rendered_blob_id is not None
    assert after.blobs[reloaded_synthesis.rendered_blob_id] == synthesis_bytes


@pytest.mark.asyncio
async def test_restart_after_success_retries_only_browser_cleanup(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, _urls = _configured(production_scenario_factory, 1, all_core=True)
    browser = BrowserTarget(set(), [])
    scenario.model_service._conversation_session_closer = browser
    await scenario.start()
    await _run_prefix(scenario, 3)

    original_close = ProductionWorkflowOrchestrator._close_completed_stage_conversation_best_effort

    async def crash_before_cleanup(
        orchestrator: ProductionWorkflowOrchestrator,
        run: Any,
        stage: SubjectProductionStage,
    ) -> None:
        if stage is SubjectProductionStage.SYNTHESIS:
            raise ProcessCrash("process lost before browser cleanup")
        await original_close(orchestrator, run, stage)

    with patch.object(
        ProductionWorkflowOrchestrator,
        "_close_completed_stage_conversation_best_effort",
        new=crash_before_cleanup,
    ):
        with pytest.raises(ProcessCrash):
            await scenario.runner.run_next()

    before = await _reload(scenario)
    conversation_id = before.run.synthesis_conversation_id
    assert conversation_id is not None
    browser.present.add(conversation_id)
    async with scenario.uow_factory() as uow:
        conversation = await uow.model_conversations.get(conversation_id)
    assert conversation is not None
    assert conversation.status is ConversationStatus.READY

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        restarted.model_service._conversation_session_closer = browser
        await restarted.enqueue_persisted_jobs(recover_abandoned=True)
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)
        # Retrying cleanup after the successful replay is safe and does not
        # change the already successful production stage.
        await restarted.model_service.archive(
            conversation_id, context_subject_id=after.run.subject_id
        )

    _assert_refetched(before, after)
    assert final.status is SubjectProductionStatus.READY
    assert restarted.model.provider_calls == []
    assert before.run.references_conversation_id is not None
    assert browser.calls == [
        before.run.references_conversation_id,
        conversation_id,
        conversation_id,
    ]
    assert browser.present == set()


@pytest.mark.asyncio
async def test_restart_during_reconciliation_preserves_exact_submission_identity(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, urls = _configured(production_scenario_factory, 1, all_core=True)
    bridge_run_id = "bridge-restart-reconciliation"
    scenario.model.script.q2(
        source_url=urls[0],
        access_mode="live_url",
        response=BridgeTransportError(
            "bridge_timeout",
            "provider received the prompt but no final answer was returned",
            retryable=True,
            phase="generation",
            submission_state="post_submission",
            bridge_run_id=bridge_run_id,
        ),
    )
    await scenario.start()
    run = await scenario.run_until_terminal()
    before = await _reload(scenario)
    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert before.run.reconciliation is not None
    model_run_id = before.run.reconciliation.model_run_id

    async with scenario.uow_factory() as uow:
        model_run = await uow.model_runs.get(model_run_id)
    assert model_run is not None
    assert model_run.status is ModelRunStatus.NEEDS_REVIEW
    assert model_run.submission_state is ModelSubmissionState.SUBMITTED_OR_UNKNOWN

    visible_text = _q2(1).replace(
        "Infrastructure observed in source 1.",
        "Infrastructure observed in source 1 during visible recovery.",
    )
    visible = VisibleRecovery(bridge_run_id, visible_text)
    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        _configure_gateway(restarted, urls, references=False, synthesis=True)
        service = ProductionReconciliationService(
            restarted.uow_factory,
            restarted.model,
            restarted.jobs,
            restarted.runner,
            bridge=visible,
        )
        preview = await service.preview_visible(restarted.run_id)
        assert preview.model_run_id == model_run_id
        assert preview.sha256
        adopted = await service.adopt_visible(
            restarted.run_id,
            preview.sha256,
            actor_id="restart-reviewer",
        )
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert adopted["model_run_id"] == str(model_run_id)
    assert final.status is SubjectProductionStatus.READY
    assert visible.previews == 2
    assert visible.releases == 1
    assert _q2_provider_calls(restarted.model) == []
    assert _provider_stages(restarted.model) == ["synthesis"]
    assert after.run.reconciliation is not None
    assert after.run.reconciliation.model_run_id == model_run_id

    async with restarted.uow_factory() as uow:
        adopted_model = await uow.model_runs.get(model_run_id)
    assert adopted_model is not None
    assert adopted_model.status is ModelRunStatus.SUCCEEDED
    assert adopted_model.raw_output_sha256 == preview.sha256


@pytest.mark.asyncio
async def test_restart_after_non_blocking_source_skip_keeps_skip_durable(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    urls = _urls(2)
    scenario = production_scenario_factory(
        _source_specs(urls, bodies={urls[0]: "", urls[1]: "ExampleRAT source-2.security-lab.io"})
    )
    scenario.restrict_core_sources(urls)
    scenario.edition.country = f"Restart Safety {scenario.edition.country_code}"
    _configure_gateway(
        scenario,
        urls,
        live_q2={urls[0]: "UNAVAILABLE", urls[1]: _q2(2)},
    )
    await scenario.start()
    await _run_prefix(scenario, 3)
    before = await _reload(scenario)
    assert before.run.current_stage is SubjectProductionStage.SYNTHESIS
    assert before.run.extraction_progress is not None
    assert {
        item["source_id"]: item["status"] for item in before.run.extraction_progress["sources"]
    } == {"S1": "skipped", "S2": "succeeded"}
    extraction_id = next(
        artifact.id
        for artifact in before.artifacts
        if artifact.stage is ProductionArtifactStage.EXTRACTION
    )

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        _configure_gateway(restarted, urls, references=False, synthesis=True)
        await restarted.enqueue_persisted_jobs()
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is SubjectProductionStatus.READY
    assert _q2_provider_calls(restarted.model) == []
    assert any(
        artifact.id == extraction_id and artifact.stage is ProductionArtifactStage.EXTRACTION
        for artifact in after.artifacts
    )
    assert after.run.extraction_progress is not None
    assert {
        item["source_id"]: item["status"] for item in after.run.extraction_progress["sources"]
    } == {"S1": "skipped", "S2": "succeeded"}

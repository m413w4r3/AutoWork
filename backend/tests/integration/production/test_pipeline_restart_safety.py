"""Production pipeline contracts when the worker process disappears."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from cti_app.application.model_gateway import ModelRequest
from cti_app.application.production_reconciliation import ProductionReconciliationService
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRunStatus,
    ModelSubmissionState,
    ModelTransport,
)
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionRunStatus,
    ProductionStage,
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


def _source_body(index: int) -> str:
    """Build an archived body a real report would plausibly have.

    The filler carries no indicator: the evidence gate still only ever sees
    ``source-<index>.security-lab.io`` and ``ExampleRAT``.
    """
    head = f"ExampleRAT source {index} source-{index}.security-lab.io was archived."
    filler = (
        " The analysed campaign is attributed to the ExampleRAT operators, whose "
        "tooling has been tracked across successive intrusion sets. The report "
        "details the delivery chain, the loader stage and the persistence "
        "mechanism observed on compromised hosts, together with the operator "
        "tradecraft seen during hands-on-keyboard activity."
    )
    return head + filler * 4


def _source_specs(
    urls: tuple[str, ...],
    *,
    bodies: dict[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    # Source checkpoints are content-addressed and the integration database is
    # session-scoped: a per-scenario capture keeps each test's model calls its own.
    capture = f" Capture {uuid4().hex}."
    return {
        url: {
            "status": 200,
            "mime": "text/plain",
            "body": (bodies or {}).get(url, _source_body(index) + capture),
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
                "reason: Coverage of the ExampleRAT activity",
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
        f"- ExampleRAT :: ExampleRAT source {index}\n\n"
        "IOC confirmed domain\n"
        f"- source-{index}.security-lab.io :: Infrastructure observed in source {index}."
    )


def _configure_gateway(
    scenario: ProductionScenario,
    urls: tuple[str, ...],
    *,
    references: bool = True,
    q2: dict[str, str | Exception] | None = None,
) -> None:
    if references:
        scenario.model.script.references(_references(urls))
    for index, url in enumerate(urls, start=1):
        scenario.model.script.q2(
            source_url=url,
            response=(q2 or {}).get(url, _q2(index)),
        )


def _configured(
    factory: ScenarioFactory,
    source_count: int,
    *,
    all_primary: bool = False,
    bodies: dict[str, str] | None = None,
) -> tuple[ProductionScenario, tuple[str, ...]]:
    urls = _urls(source_count)
    scenario = factory(_source_specs(urls, bodies=bodies))
    scenario.edition.country = f"Restart Safety {scenario.edition.country_code}"
    if all_primary:
        scenario.restrict_core_sources(urls)
        scenario.set_source_roles({url: SourceRole.PRIMARY for url in urls})
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
        run = await uow.production_runs.get(scenario.run_id)
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


def _q2_provider_calls(model: ScriptedModelGateway) -> list[str]:
    """The scripted source URLs whose archived capture reached the provider."""
    return [
        url
        for request in model.provider_calls
        if request.prompt_template_id == "production-extraction-archive-source"
        and (url := model.script.source_url_for(request.metadata.get("source_content_sha256")))
    ]


def _provider_stages(model: ScriptedModelGateway) -> list[str]:
    stages: list[str] = []
    for request in model.provider_calls:
        if request.prompt_template_id.startswith("production-extraction"):
            stages.append("extraction")
        elif request.routing_hint.value == "web_research":
            stages.append("references")
        elif request.prompt_template_id == "production-synthesis":
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
    assert before.run.current_stage is ProductionStage.REFERENCES
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
    assert final.status is ProductionRunStatus.READY
    assert after.run.current_stage is ProductionStage.ASSEMBLY
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
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
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
    assert before.run.current_stage is ProductionStage.EXTRACTION

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        # Deliberately do not configure a References answer. A call would be
        # an assertion failure in the fake adapter.
        _configure_gateway(restarted, urls, references=False)
        await restarted.enqueue_persisted_jobs()
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is ProductionRunStatus.READY
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
    scenario, urls = _configured(production_scenario_factory, 3, all_primary=True)
    await scenario.start()
    await _run_prefix(scenario, 2)

    original_draft = scenario.model.draft
    extraction_calls = 0

    async def crash_before_third_source(request: ModelRequest, *args: Any, **kwargs: Any) -> Any:
        nonlocal extraction_calls
        extraction_calls += 1
        if extraction_calls == 3:
            raise ProcessCrash("process lost before the third source")
        return await original_draft(request, *args, **kwargs)

    with patch.object(scenario.model, "draft", new=crash_before_third_source):
        with pytest.raises(ProcessCrash):
            await scenario.runner.run_next()

    before = await _reload(scenario)
    assert _q2_provider_calls(scenario.model) == list(urls[:2])
    completed_runs = {
        call.model_run_id for call in scenario.model.calls if call.stage == "extraction"
    }
    async with scenario.uow_factory() as uow:
        checkpoints = [
            row
            for url in urls[:2]
            for row in await uow.source_extractions.list_for_url(url)
            if row.model_run_id in completed_runs
        ]
    # Only the two answered captures reached a durable checkpoint.
    assert len(checkpoints) == 2

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        _configure_gateway(restarted, urls, references=False)
        await restarted.enqueue_persisted_jobs(recover_abandoned=True)
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is ProductionRunStatus.READY
    # The two durable checkpoints are reused; only the third capture is sent.
    assert _q2_provider_calls(restarted.model) == [urls[2]]

    extraction = next(
        artifact
        for artifact in after.artifacts
        if artifact.stage is ProductionArtifactStage.EXTRACTION
    )
    assert extraction.canonical_blob_id is not None
    payload = json.loads(after.blobs[extraction.canonical_blob_id])
    by_url = {source["canonical_url"]: source for source in payload["sources"]}
    for index, url in enumerate(urls, start=1):
        assert [item["value"] for item in by_url[url]["indicators"]] == [
            f"source-{index}.security-lab.io"
        ]
    assert [by_url[url]["reuse_state"] for url in urls] == ["reused", "reused", "fresh"]


@pytest.mark.asyncio
async def test_restart_after_synthesis_runs_editorial_enrichment_and_assembly(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, _urls = _configured(production_scenario_factory, 1, all_primary=True)
    await scenario.start()
    await _run_prefix(scenario, 4)
    before = await _reload(scenario)
    assert before.run.current_stage is ProductionStage.EDITORIAL_ENRICHMENT
    synthesis = next(
        artifact
        for artifact in before.artifacts
        if artifact.stage is ProductionArtifactStage.SYNTHESIS
    )
    assert synthesis.rendered_blob_id is not None
    synthesis_bytes = before.blobs[synthesis.rendered_blob_id]

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        # The persisted synthesis is reused; enrichment drafts once after restart.
        await restarted.enqueue_persisted_jobs()
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is ProductionRunStatus.READY
    assert [request.prompt_template_id for request in restarted.model.provider_calls] == [
        "production-editorial-enrichment"
    ]
    reloaded_synthesis = next(
        artifact
        for artifact in after.artifacts
        if artifact.stage is ProductionArtifactStage.SYNTHESIS
    )
    assert reloaded_synthesis.rendered_blob_id is not None
    assert after.blobs[reloaded_synthesis.rendered_blob_id] == synthesis_bytes


@pytest.mark.asyncio
async def test_restart_during_reconciliation_preserves_exact_submission_identity(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    scenario, urls = _configured(production_scenario_factory, 1, all_primary=True)
    scenario.model.use_chatgpt_bridge_identity()
    bridge_run_id = "bridge-restart-reconciliation"
    scenario.model.script.q2(
        source_url=urls[0],
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
    assert run.status is ProductionRunStatus.NEEDS_REVIEW
    assert before.run.reconciliation is not None
    model_run_id = before.run.reconciliation.model_run_id

    async with scenario.uow_factory() as uow:
        model_run = await uow.model_runs.get(model_run_id)
    assert model_run is not None
    assert model_run.provider is ModelProvider.OPENAI
    assert model_run.backend is ModelBackend.CHATGPT_BRIDGE
    assert model_run.transport is ModelTransport.OPENAI_RESPONSES
    assert model_run.status is ModelRunStatus.NEEDS_REVIEW
    assert model_run.submission_state is ModelSubmissionState.EXTERNAL_STATE_UNKNOWN

    # The operator adopts the line-oriented answer the provider produced.
    visible_text = _q2(1).replace(
        "Infrastructure observed in source 1.",
        "Infrastructure observed in source 1 during visible recovery.",
    )
    visible = VisibleRecovery(bridge_run_id, visible_text)
    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        _configure_gateway(restarted, urls, references=False)
        restarted.model.use_chatgpt_bridge_identity()
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
    assert final.status is ProductionRunStatus.READY
    assert visible.previews == 2
    assert visible.releases == 1
    assert _q2_provider_calls(restarted.model) == []
    assert _provider_stages(restarted.model) == ["synthesis"]
    assert after.run.reconciliation is not None
    assert after.run.reconciliation.model_run_id == model_run_id

    async with restarted.uow_factory() as uow:
        adopted_model = await uow.model_runs.get(model_run_id)
    assert adopted_model is not None
    assert adopted_model.provider is ModelProvider.OPENAI
    assert adopted_model.backend is ModelBackend.CHATGPT_BRIDGE
    assert adopted_model.transport is ModelTransport.OPENAI_RESPONSES
    assert adopted_model.status is ModelRunStatus.SUCCEEDED
    assert adopted_model.raw_output_sha256 == preview.sha256


@pytest.mark.asyncio
async def test_restart_after_non_blocking_source_skip_keeps_skip_durable(
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
) -> None:
    urls = _urls(2)
    # The SUPPORTING capture is archived without readable text.
    scenario = production_scenario_factory(_source_specs(urls, bodies={urls[0]: ""}))
    scenario.restrict_core_sources((urls[1],))
    scenario.edition.country = f"Restart Safety {scenario.edition.country_code}"
    _configure_gateway(scenario, urls)
    await scenario.start()
    await _run_prefix(scenario, 3)
    before = await _reload(scenario)
    assert before.run.current_stage is ProductionStage.SYNTHESIS
    assert before.run.extraction_progress is not None
    statuses = {urls[0]: "failed", urls[1]: "succeeded"}
    assert {
        item["canonical_url"]: item["status"] for item in before.run.extraction_progress["sources"]
    } == statuses
    extraction_id = next(
        artifact.id
        for artifact in before.artifacts
        if artifact.stage is ProductionArtifactStage.EXTRACTION
    )

    async with _fresh_runtime(scenario, migrated_postgres_url) as restarted:
        _configure_gateway(restarted, urls, references=False)
        await restarted.enqueue_persisted_jobs()
        final = await restarted.run_until_terminal()
        after = await _reload(restarted)

    _assert_refetched(before, after)
    assert final.status is ProductionRunStatus.READY
    assert _q2_provider_calls(restarted.model) == []
    assert any(
        artifact.id == extraction_id and artifact.stage is ProductionArtifactStage.EXTRACTION
        for artifact in after.artifacts
    )
    assert after.run.extraction_progress is not None
    assert {
        item["canonical_url"]: item["status"] for item in after.run.extraction_progress["sources"]
    } == statuses

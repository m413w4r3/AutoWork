from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    ReprocessDiscoveryReportParameters,
    discovery_conversation_id,
    discovery_job_idempotency_key,
    discovery_request_hash,
    discovery_request_snapshot,
    discovery_research_model_run_id,
)
from cti_app.application.discovery.jobs import (
    DISCOVERY_JOB_KIND,
    REPROCESS_DISCOVERY_REPORT_JOB_KIND,
)
from cti_app.application.discovery.prompts import _research_prompt
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.jobs import (
    DuplicateJobError,
    JobExecutionContext,
    JobExecutor,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelGateway,
    ModelRouter,
    SafeModelRequest,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    DiscoveryBatch,
    DiscoveryRun,
    DiscoveryRunInputMode,
    DiscoverySourceMode,
    SourceRelationshipStatus,
    SourceRole,
    SourceVerificationStatus,
)
from cti_app.domain.discovery_cumulative import DiscoveryInputMode
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelUsage,
)
from cti_app.integrations.models import (
    BridgeTransportError,
    FakeModelAdapter,
    InMemoryModelOutputStore,
)
from tests.discovery_support import InMemoryDiscoveryUnitOfWorkFactory
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.model_support import InMemoryModelRunUnitOfWorkFactory


class FakeBridgeCapabilities:
    async def capabilities(self) -> dict[str, object]:
        return {
            "web_search": True,
            "native_sources": False,
            "visible_citations": True,
        }

    async def archive_conversation(self, conversation_id: UUID) -> None:
        del conversation_id

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, object]:
        del bridge_run_id
        return {}


class OrderTrackingBridgeCapabilities:
    """Records, at the moment archive_conversation is called, whether the batch
    was already persisted — the only way to check orchestration order through
    DiscoveryService's public surface instead of reading private implementation
    lines. The bridge conversation itself is a live browser session with no
    application-side lifecycle row: only the durable batch state is checked."""

    def __init__(self, discovery_uow: InMemoryDiscoveryUnitOfWorkFactory) -> None:
        self._discovery_uow = discovery_uow
        self.archive_calls: list[UUID] = []
        self.archive_call_batch_persisted: list[bool] = []

    async def capabilities(self) -> dict[str, object]:
        return {"web_search": True, "native_sources": False, "visible_citations": True}

    async def archive_conversation(self, conversation_id: UUID) -> None:
        self.archive_calls.append(conversation_id)
        self.archive_call_batch_persisted.append(bool(self._discovery_uow.state))

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, object]:
        del bridge_run_id
        return {}


class TransientResearchAdapter(FakeModelAdapter):
    async def invoke(
        self,
        request: SafeModelRequest,
        *,
        role: ModelRole,
        output_schema: type[BaseModel] | None = None,
    ) -> AdapterResult:
        del role, output_schema
        self.calls.append(request)
        raise BridgeTransportError(
            "bridge_unreachable",
            "Bridge temporairement indisponible.",
            retryable=True,
        )


class DeferredResearchAdapter(FakeModelAdapter):
    def __init__(
        self,
        *,
        pending_resumes: int = 0,
        terminal_error: BridgeTransportError | None = None,
        needs_review: bool = False,
    ) -> None:
        super().__init__(research_text=research_markdown_fixture())
        self.pending_resumes = pending_resumes
        self.terminal_error = terminal_error
        self.needs_review = needs_review
        self.resume_calls = 0

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: type[BaseModel] | None = None,
    ) -> AdapterResult:
        del role, output_schema
        self.resume_calls += 1
        if self.terminal_error is not None:
            raise self.terminal_error
        if self.needs_review:
            return AdapterResult(
                status=AdapterResultStatus.NEEDS_REVIEW,
                provider=self.provider,
                requested_model=self.requested_model,
                actual_model_version=self.requested_model,
                response_id=response_id,
                usage=ModelUsage(),
                metadata={
                    "reason": "no_final_answer",
                    "conversation": {
                        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "external_locator": (
                            "https://chatgpt.com/c/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                        ),
                        "assistant_turns_before": 1,
                    },
                    "completion_signal": "assistant_actions",
                    "completion_confidence": "high",
                    "output_chars": 0,
                },
            )
        if self.pending_resumes > 0:
            self.pending_resumes -= 1
            return AdapterResult(
                status=AdapterResultStatus.WAITING_BACKGROUND,
                provider=self.provider,
                requested_model=self.requested_model,
                actual_model_version=self.requested_model,
                response_id=response_id,
                usage=ModelUsage(),
            )
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            response_id=response_id,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            output_text=research_markdown_fixture(),
        )


def gateway_for_adapter(
    adapter: FakeModelAdapter,
) -> tuple[ModelGateway, InMemoryModelRunUnitOfWorkFactory, InMemoryModelOutputStore]:
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_backend=ModelBackend.FAKE,
        ),
        model_uow,
        output_store,
    )
    return gateway, model_uow, output_store


def persisted_research_run(
    params: DiscoverEditionParameters,
    *,
    status: ModelRunStatus,
    output_reference: str | None = None,
) -> ModelRun:
    request_hash = discovery_request_hash(params)
    run = ModelRun(
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake-deterministic-v1",
        prompt_template_id="monthly-cti-discovery",
        prompt_template_version="4.0",
        authorized_input_hash="a" * 64,
        evidence_pack_hash=request_hash,
        parameters={},
        id=discovery_research_model_run_id(params.discovery_run_id),
    )
    if status is ModelRunStatus.WAITING_BACKGROUND:
        run.wait_for_background(
            response_id="bridge-durable-run",
            actual_model_version="chatgpt-web",
            usage=ModelUsage(),
        )
    elif status is ModelRunStatus.SUCCEEDED and output_reference is not None:
        run.succeed(
            actual_model_version="chatgpt-web",
            duration_ms=180_000,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            output_references=(output_reference,),
            response_id="bridge-durable-run",
        )
    return run


def research_markdown_fixture() -> str:
    return """# SUJETS CANDIDATS

## SUBJECT S1
title: MuddyWater déploie une nouvelle chaîne d'infection
presentation: Une campagne expose une chaîne technique documentée.
actor_or_campaign: MuddyWater
technical_potential: 4
technical_potential_reason: Configurations et règles sont annoncées.
artifacts: ioc, samples, configurations, yara
uncertainties: Attribution reprise de la source; victimologie incomplète

### PUBLICATION P1
title: MuddyWater technical report
url: https://vendor.example/reports/muddywater?utm_source=feed
publisher: Vendor Research
published_at: 2026-07-10
period_relation: in_period
source_role: primary
ioc_presence: declared
ioc_declared_count: 42
ioc_visible_count: unknown

### PUBLICATION P2
title: A new MuddyWater campaign
url: https://relay.example/news/muddywater
publisher: Security News
published_at: 2026-07-11
period_relation: in_period
source_role: relay
ioc_presence: none
ioc_declared_count: unknown
ioc_visible_count: unknown

### PUBLICATION P3
title: CERT advisory on the campaign
url: https://cert.example/advisories/42
publisher: National CERT
published_at: 2026-07-12
period_relation: in_period
source_role: independent
ioc_presence: visible
ioc_declared_count: unknown
ioc_visible_count: 3

# LIMITES
Recherche non exhaustive.
"""


def parameters(axis: str = "initial") -> DiscoverEditionParameters:
    return DiscoverEditionParameters(
        edition_id=uuid4(),
        discovery_run_id=uuid4(),
        country="Iran",
        country_code="IR",
        country_aliases=["Iran", "IR", "République islamique d'Iran"],
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        languages=["fr", "en", "fa"],
        source_profile="iran-default",
        keywords=["APT", "IOC"],
        exclusions=["cryptomonnaie"],
        complementary_axis=axis,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


async def test_complete_discovery_job_with_fake_adapter_is_sourced_and_idempotent() -> None:
    fake = FakeModelAdapter(
        research_text=research_markdown_fixture(),
        structured_outputs={
            "ResearchBatch": (
                '{"minimal_example":{"citations":[],"queries":[],"topics":[]},'
                '"version":"research-batch-compact-v1"}'
            )
        },
    )
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
            forced_backend=ModelBackend.FAKE,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    grouped_editions: list[UUID] = []

    async def reconcile_after_batch(
        batch: DiscoveryBatch, input_mode: object, actor_id: str
    ) -> None:
        del input_mode, actor_id
        grouped_editions.append(batch.edition_id)

    discovery = DiscoveryService(
        discovery_uow,
        gateway,
        archive=gateway,
        bridge_capabilities_provider=FakeBridgeCapabilities(),
        after_persisted_batch=reconcile_after_batch,
    )
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()

    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="test-discovery",
        input_parameters=params.model_dump(mode="json"),
    )
    await dispatcher.dispatch(job.id)

    completed = await jobs.get(job.id)
    assert completed.status is JobStatus.SUCCEEDED
    batches = await discovery.list_batches(params.edition_id)
    assert len(batches) == 1
    assert len(batches[0].candidates) == 1
    canonical_candidates = await discovery.list_candidates_for_edition(params.edition_id)
    assert [item.id for item in canonical_candidates] == [batches[0].candidates[0].id]
    original_title = canonical_candidates[0].title
    batches[0].candidates[0].title = "Artificial batch-only mutation"
    reread_candidates = await discovery.list_candidates_for_edition(params.edition_id)
    assert reread_candidates[0].id == canonical_candidates[0].id
    assert reread_candidates[0].title == original_title
    assert batches[0].source_mode is DiscoverySourceMode.MODEL_DECLARED_URLS
    assert batches[0].source_coverage_complete is False
    assert batches[0].citation_count == 0
    assert batches[0].bridge_capabilities == {
        "web_search": True,
        "native_sources": False,
        "visible_citations": True,
        "snapshot_available": True,
    }
    candidate = batches[0].candidates[0]
    assert {source.role for source in candidate.sources} == {
        SourceRole.PRIMARY,
        SourceRole.RELAY,
        SourceRole.INDEPENDENT,
    }
    assert all(
        source.verification_status is SourceVerificationStatus.UNVERIFIED
        for source in candidate.sources
    )
    assert all(
        source.relationship_status is SourceRelationshipStatus.PROVISIONAL
        for source in candidate.sources
    )
    assert len({source.canonical_url for source in candidate.sources}) == len(candidate.sources)
    assert len(fake.calls) == 1
    assert fake.calls[0].conversation is not None
    assert fake.calls[0].conversation.mode == "fresh"
    assert all(run.model_role.value == "research" for run in model_uow.state.values())
    assert grouped_editions == [params.edition_id]

    with pytest.raises(DuplicateJobError):
        await jobs.submit(
            kind=DISCOVERY_JOB_KIND,
            aggregate_type="discovery_run",
            aggregate_id=params.discovery_run_id,
            idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
            correlation_id="test-retry",
            input_parameters=params.model_dump(mode="json"),
        )
    assert len(await discovery.list_batches(params.edition_id)) == 1
    assert len(fake.calls) == 1
    immutable_first_batch = deepcopy((await discovery.list_batches(params.edition_id))[0])

    complementary = params.model_copy(update={"discovery_run_id": uuid4()})
    second_job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=complementary.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(complementary.discovery_run_id),
        correlation_id="test-complement",
        input_parameters=complementary.model_dump(mode="json"),
    )
    await dispatcher.dispatch(second_job.id)
    batches = await discovery.list_batches(params.edition_id)
    assert len(batches) == 2
    assert sum(len(batch.candidates) for batch in batches) == 2
    assert batches[0] == immutable_first_batch
    assert len(fake.calls) == 2
    assert discovery_request_hash(params) == discovery_request_hash(complementary)
    assert batches[0].discovery_model_run_id != batches[1].discovery_model_run_id
    assert batches[0].id != batches[1].id
    assert fake.calls[1].conversation is not None
    assert fake.calls[1].conversation.mode == "fresh"
    assert fake.calls[1].conversation.id != fake.calls[0].conversation.id
    assert grouped_editions == [params.edition_id, params.edition_id]


async def test_successful_discovery_archives_conversation_once_after_batch_persisted() -> None:
    """Orchestration boundary domain tests can't cover: the batch must be
    durably persisted as the canonical successful result before the exact live
    Temporary Chat browser session is ever requested for closure — and closed
    exactly once."""
    fake = FakeModelAdapter(research_text=research_markdown_fixture())
    gateway, _, _ = gateway_for_adapter(fake)
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    capabilities = OrderTrackingBridgeCapabilities(discovery_uow)
    discovery = DiscoveryService(
        discovery_uow,
        gateway,
        archive=gateway,
        bridge_capabilities_provider=capabilities,
    )
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()
    expected_conversation_id = discovery_conversation_id(params.discovery_run_id)

    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="lifecycle-success",
        input_parameters=params.model_dump(mode="json"),
    )
    await dispatcher.dispatch(job.id)

    completed = await jobs.get(job.id)
    assert completed.status is JobStatus.SUCCEEDED

    # Same conversation id used for the model request and for the archive call.
    assert fake.calls[0].conversation is not None
    assert fake.calls[0].conversation.id == expected_conversation_id

    # The archive request fires exactly once, and only once the batch is durably
    # persisted as the canonical successful result.
    assert capabilities.archive_calls == [expected_conversation_id]
    assert capabilities.archive_call_batch_persisted == [True]


async def test_failed_research_never_deletes_conversation_or_batch() -> None:
    """Characterizes current behavior when research fails before the batch is
    persisted: the mission must never be treated as a success, so the
    conversation is neither archived nor marked for deletion."""
    adapter = TransientResearchAdapter()
    gateway, _, _ = gateway_for_adapter(adapter)
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    capabilities = OrderTrackingBridgeCapabilities(discovery_uow)
    discovery = DiscoveryService(
        discovery_uow,
        gateway,
        archive=gateway,
        bridge_capabilities_provider=capabilities,
    )
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()

    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="lifecycle-failure",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )
    await dispatcher.dispatch(job.id)

    failed = await jobs.get(job.id)
    assert failed.status is JobStatus.FAILED
    assert await discovery.list_batches(params.edition_id) == []
    # SUCCESS was never reached, so the temporary browser session is never
    # requested for closure — it stays alive for inspection/recovery.
    assert capabilities.archive_calls == []


async def test_needs_review_keeps_conversation_recoverable_without_archiving() -> None:
    """A NEEDS_REVIEW research outcome must not be treated as success either:
    the conversation stays available for a human to resume it, and is not
    archived or scheduled for deletion in the meantime."""
    params = parameters()
    adapter = DeferredResearchAdapter(needs_review=True)
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    waiting = persisted_research_run(params, status=ModelRunStatus.WAITING_BACKGROUND)
    model_uow.state[waiting.id] = waiting
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    capabilities = OrderTrackingBridgeCapabilities(discovery_uow)
    discovery = DiscoveryService(
        discovery_uow,
        gateway,
        archive=gateway,
        bridge_capabilities_provider=capabilities,
    )
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="lifecycle-needs-review",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    first = await jobs.get(job.id)
    assert first.status is JobStatus.WAITING_HUMAN
    assert await discovery.list_batches(params.edition_id) == []
    # A NEEDS_REVIEW outcome is not success: the temporary browser session
    # stays available for a human to resume it, never closed here.
    assert capabilities.archive_calls == []


async def test_discovery_renews_job_heartbeat_while_bridge_remains_running() -> None:
    adapter = DeferredResearchAdapter(pending_resumes=4)
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    job_uow = InMemoryJobUnitOfWorkFactory()
    recovery_passes: list[list[object]] = []
    heartbeat_values: list[datetime] = []

    async def poll_cycle(_: float) -> None:
        running = next(iter(job_uow.state.values()))
        # Simule un job démarré depuis bien plus de trois anciens timeouts,
        # tout en laissant son bail courant être renouvelé par le poller.
        running.started_at = datetime.now(UTC) - timedelta(minutes=10)
        assert running.heartbeat_at is not None
        heartbeat_values.append(running.heartbeat_at)
        recovery_passes.append(list(await jobs.recover_abandoned(timedelta(seconds=120))))

    discovery = DiscoveryService(
        discovery_uow,
        gateway,
        archive=gateway,
        background_poll_interval_seconds=5,
        background_waiter=poll_cycle,
    )
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="durable-heartbeat",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    completed = await jobs.get(job.id)
    run = next(iter(model_uow.state.values()))
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.progress_current == completed.progress_total == 4
    assert adapter.resume_calls == 5
    assert len(adapter.calls) == 1
    assert run.status is ModelRunStatus.SUCCEEDED
    assert run.response_id is not None
    assert recovery_passes == [[], [], [], []]
    assert heartbeat_values == sorted(heartbeat_values)
    assert len(await discovery.list_batches(params.edition_id)) == 1


async def test_worker_restart_resumes_waiting_model_run_by_get_without_second_post() -> None:
    params = parameters()
    adapter = DeferredResearchAdapter()
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    waiting = persisted_research_run(params, status=ModelRunStatus.WAITING_BACKGROUND)
    model_uow.state[waiting.id] = waiting
    discovery = DiscoveryService(
        InMemoryDiscoveryUnitOfWorkFactory(),
        gateway,
        archive=gateway,
        background_waiter=lambda _: _completed_wait(),
    )
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="worker-restart",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )
    persisted_job = job_uow.state[job.id]
    persisted_job.start(datetime.now(UTC) - timedelta(minutes=5))
    persisted_job.report_progress(
        2,
        4,
        "ChatGPT recherche et analyse les sources",
        datetime.now(UTC) - timedelta(minutes=3),
    )

    recovered = await jobs.recover_abandoned(timedelta(seconds=120))
    await dispatcher.dispatch(job.id)

    completed = await jobs.get(job.id)
    assert [item.id for item in recovered] == [job.id]
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.attempt == completed.max_attempts == 1
    assert adapter.calls == []
    assert adapter.resume_calls == 1
    assert len(model_uow.state) == 1


async def _completed_wait() -> None:
    return None


async def test_completed_model_run_is_reparsed_after_resume_without_bridge_call() -> None:
    params = parameters()
    adapter = DeferredResearchAdapter()
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    reference = await output_store.store(
        research_markdown_fixture().encode(), mime_type="text/markdown"
    )
    completed_run = persisted_research_run(
        params,
        status=ModelRunStatus.SUCCEEDED,
        output_reference=reference,
    )
    model_uow.state[completed_run.id] = completed_run
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="completed-resume",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    assert (await jobs.get(job.id)).status is JobStatus.SUCCEEDED
    assert adapter.calls == []
    assert adapter.resume_calls == 0


async def test_incomplete_model_run_waits_for_human_without_automatic_relaunch() -> None:
    params = parameters()
    adapter = DeferredResearchAdapter(needs_review=True)
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    waiting = persisted_research_run(params, status=ModelRunStatus.WAITING_BACKGROUND)
    model_uow.state[waiting.id] = waiting
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="incomplete-review",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)
    first = await jobs.get(job.id)
    await dispatcher.dispatch(job.id)
    unchanged = await jobs.get(job.id)

    assert first.status is JobStatus.WAITING_HUMAN
    assert unchanged.status is JobStatus.WAITING_HUMAN
    assert first.user_message == (
        "ChatGPT s'est arrêté sans produire de réponse finale. "
        "La conversation a été conservée et peut être reprise."
    )
    assert first.error_details is not None
    assert first.error_details["reason"] == "no_final_answer"
    assert model_uow.state[waiting.id].status is ModelRunStatus.NEEDS_REVIEW
    assert adapter.calls == []
    assert adapter.resume_calls == 1


async def test_manual_recovery_archives_exact_report_and_resumes_original_job() -> None:
    params = parameters()
    adapter = DeferredResearchAdapter(needs_review=True)
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    waiting = persisted_research_run(params, status=ModelRunStatus.WAITING_BACKGROUND)
    model_uow.state[waiting.id] = waiting
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    discovery = DiscoveryService(discovery_uow, gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="manual-recovery",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )
    await dispatcher.dispatch(job.id)
    assert (await jobs.get(job.id)).status is JobStatus.WAITING_HUMAN

    markdown = research_markdown_fixture() + "\n<!-- exact manual import -->\n"
    preview = await discovery.preview_manual_recovery(params, waiting.id, markdown)
    await discovery.adopt_recovery_report(
        params,
        waiting.id,
        markdown,
        expected_sha256=preview["sha256"],
        provenance="manual_import",
        actor_id="analyst:test",
    )
    await jobs.resume_waiting_human(job.id, actor_id="analyst:test")
    await dispatcher.dispatch(job.id)

    completed = await jobs.get(job.id)
    recovered = model_uow.state[waiting.id]
    assert completed.status is JobStatus.SUCCEEDED
    assert recovered.status is ModelRunStatus.SUCCEEDED
    assert recovered.raw_output_reference is not None
    assert (
        await output_store.read(recovered.raw_output_reference, max_bytes=10_000_000)
    ).decode() == markdown
    assert recovered.error_details is not None
    assert recovered.error_details["recovery"]["provenance"] == "manual_import"
    batches = await discovery.list_batches(params.edition_id)
    assert len(batches) == 1
    assert batches[0].parsing_revision == 1


async def test_controlled_completion_is_idempotent_and_keeps_exact_conversation() -> None:
    params = parameters()
    adapter = FakeModelAdapter(research_text=research_markdown_fixture())
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    parent = persisted_research_run(params, status=ModelRunStatus.WAITING_BACKGROUND)
    parent.require_review(
        "no_final_answer",
        "incomplete",
        details={
            "conversation": {
                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "initial_assistant_turn_id": "external-turn-before-incomplete",
                # Diagnostic only: never used to route or reopen the conversation.
                "external_locator": "https://chatgpt.com/?temporary-chat=true",
            }
        },
    )
    model_uow.state[parent.id] = parent
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)

    first = await discovery.start_completion_recovery(params, parent.id)
    second = await discovery.start_completion_recovery(params, parent.id)

    assert first == second
    assert len(adapter.calls) == 1
    submitted = adapter.calls[0]
    assert submitted.text == (
        "Ta réponse précédente ne contient pas de résultat final. Termine maintenant "
        "la mission initiale et fournis directement le rapport Markdown demandé, sans "
        "recommencer toute la recherche."
    )
    assert submitted.conversation is not None
    assert submitted.conversation.mode == "continue"
    assert submitted.conversation.id == UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert submitted.conversation.expected_turn_id == "external-turn-before-incomplete"
    assert submitted.parameters["bridge_recovery"] is True
    details = model_uow.state[parent.id].error_details
    assert details is not None
    assert details["recovery_child_model_run_id"] == str(first)


async def test_terminal_bridge_error_fails_discovery_without_parsing() -> None:
    params = parameters()
    adapter = DeferredResearchAdapter(
        terminal_error=BridgeTransportError(
            "bridge_extension_disconnected",
            "L'extension ChatGPT est déconnectée.",
            retryable=True,
        )
    )
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    waiting = persisted_research_run(params, status=ModelRunStatus.WAITING_BACKGROUND)
    model_uow.state[waiting.id] = waiting
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="terminal-bridge-error",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    failed = await jobs.get(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "bridge_extension_disconnected"
    assert model_uow.state[waiting.id].status is ModelRunStatus.FAILED
    assert adapter.calls == []
    assert adapter.resume_calls == 1


async def test_human_cancellation_stops_background_polling_without_resubmission() -> None:
    adapter = DeferredResearchAdapter(pending_resumes=2)
    gateway, _, _ = gateway_for_adapter(adapter)
    job_uow = InMemoryJobUnitOfWorkFactory()
    cancellation_requested = False

    async def cancel_during_wait(_: float) -> None:
        nonlocal cancellation_requested
        if not cancellation_requested:
            cancellation_requested = True
            running = next(iter(job_uow.state.values()))
            await jobs.cancel(running.id, actor_id="analyst")

    discovery = DiscoveryService(
        InMemoryDiscoveryUnitOfWorkFactory(),
        gateway,
        archive=gateway,
        background_waiter=cancel_during_wait,
    )
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="cancel-background",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    cancelled = await jobs.get(job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert len(adapter.calls) == 1
    assert adapter.resume_calls == 1


async def test_partial_invalid_output_keeps_topics_and_archives_diagnostics() -> None:
    fake = FakeModelAdapter(
        research_text=research_markdown_fixture()
        + """
## SUBJECT S2
title: Sujet incomplet
presentation: Publication conservée malgré une URL absente.
technical_potential: unknown
champ_surprise: valeur
### PUBLICATION P4
title: Sans URL
url: ftp://invalid.example/report
""",
    )
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
        ),
        model_uow,
        output_store,
    )
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="partial-output",
        input_parameters=params.model_dump(mode="json"),
    )

    await dispatcher.dispatch(job.id)

    completed = await jobs.get(job.id)
    assert completed.status is JobStatus.SUCCEEDED
    batches = await discovery.list_batches(params.edition_id)
    assert batches[0].candidates
    assert len(fake.calls) == 1
    assert all(run.model_role.value == "research" for run in model_uow.state.values())
    research_run = next(iter(model_uow.state.values()))
    assert research_run.raw_output_reference
    assert research_run.parser_stage == "report_parsing_partial"
    assert research_run.validation_errors


async def test_totally_invalid_output_is_archived_before_safe_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_output = "```json\n{invalid\\_json:SECRET_MODEL_OUTPUT}\n```"
    fake = FakeModelAdapter(
        research_text=raw_output,
    )
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
        ),
        model_uow,
        output_store,
    )
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="invalid-output",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    failed = await jobs.get(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "report_schema_unsupported"
    assert failed.error_details is not None
    assert failed.error_details["phase"] == "local_parsing"
    research_run = next(iter(model_uow.state.values()))
    assert research_run.raw_output_reference in research_run.output_references
    assert output_store.objects
    assert "SECRET_MODEL_OUTPUT" not in caplog.text
    assert raw_output not in caplog.text


async def test_transient_job_error_never_creates_a_second_research() -> None:
    fake = TransientResearchAdapter()
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=fake,
            openai_structured=fake,
            qwen=fake,
            fake=fake,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    params = parameters()
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=params.discovery_run_id,
        idempotency_key=discovery_job_idempotency_key(params.discovery_run_id),
        correlation_id="transient-single-attempt",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)
    await dispatcher.dispatch(job.id)

    failed = await jobs.get(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.attempt == failed.max_attempts == 1
    assert len(fake.calls) == 1
    assert len(model_uow.state) == 1


def test_research_prompt_is_the_documented_markdown_contract() -> None:
    prompt = _research_prompt(
        parameters().model_copy(
            update={
                "country_aliases": [
                    "Iran",
                    "IR",
                    "ir",
                    "République islamique d'Iran",
                    "république islamique d'iran",
                ],
                "languages": ["fr", "EN", "en", "fa"],
                "period_end": date(2027, 7, 31),
                "as_of_date": date(2026, 8, 13),
            }
        )
    )

    assert "# SUJETS CANDIDATS" in prompt
    assert "## SUBJECT S1" in prompt
    assert "### PUBLICATION P1" in prompt
    assert "Date de recherche : 2026-08-13" in prompt
    assert "Période demandée : 2026-07-01 au 2027-07-31" in prompt
    assert "Période observable : 2026-07-01 au 2026-08-13" in prompt
    assert "Iran (alias : IR, République islamique d'Iran)" in prompt
    assert "Langues de travail de l'édition : fr, EN, fa" in prompt
    # La langue ne doit jamais restreindre la recherche : les langues de
    # l'édition restent une information de contexte, pas un filtre.
    assert "La langue n'est jamais un critère de sélection" in prompt
    assert "le japonais" in prompt
    assert "l'espagnol" in prompt
    assert "le coréen" in prompt
    assert "Il n’existe aucune limite ni" in prompt  # noqa: RUF001
    assert "N’invente aucune URL, date, attribution" in prompt  # noqa: RUF001
    assert "visible-iocs: <jusqu’à 10 valeurs exactes" in prompt  # noqa: RUF001
    assert "period: <" not in prompt
    assert "N’échappe pas les tirets des noms de champs." in prompt  # noqa: RUF001


def _import_service() -> tuple[DiscoveryService, InMemoryDiscoveryUnitOfWorkFactory, list[UUID]]:
    """Service de découverte dont l'adaptateur modèle échouerait s'il était appelé.

    L'import d'une réponse ChatGPT existante ne doit provoquer aucun appel modèle.
    """
    adapter = TransientResearchAdapter()
    gateway, _, _ = gateway_for_adapter(adapter)
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    grouped: list[UUID] = []

    async def after_persisted_batch(
        batch: DiscoveryBatch, input_mode: object, actor_id: str
    ) -> None:
        del input_mode, actor_id
        grouped.append(batch.edition_id)

    service = DiscoveryService(
        discovery_uow,
        gateway,
        archive=gateway,
        bridge_capabilities_provider=FakeBridgeCapabilities(),
        after_persisted_batch=after_persisted_batch,
    )
    return service, discovery_uow, grouped


async def test_standalone_import_creates_batch_without_job_or_model_call() -> None:
    """§32.5 : import initial sans job ni ModelRun préalable."""
    service, _, grouped = _import_service()
    params = parameters(axis="manual-import")
    markdown = research_markdown_fixture()

    preview = await service.preview_standalone_import(params, markdown)

    assert preview["subject_count"] >= 1
    assert preview["publication_count"] >= 1

    batch, reused, _reconciliation_job_id = await service.import_standalone_report(
        params,
        markdown,
        expected_sha256=preview["sha256"],
        actor_id="dev-analyst",
        idempotency_key="manual-import-1",
    )

    assert reused is False
    assert batch.source_mode is DiscoverySourceMode.MANUAL_IMPORT
    assert batch.candidates
    assert grouped == [params.edition_id]

    # Le rapport reste relisible comme n'importe quelle contribution archivée.
    archived = await service.read_archived_report(params.edition_id, batch.discovery_model_run_id)
    assert archived == markdown


async def test_standalone_import_is_idempotent_on_identical_markdown() -> None:
    """§32.6 : réimporter exactement le même Markdown ne crée pas de doublon."""
    service, _, _ = _import_service()
    params = parameters(axis="manual-import")
    markdown = research_markdown_fixture()
    digest = (await service.preview_standalone_import(params, markdown))["sha256"]

    first, reused_first, _first_job_id = await service.import_standalone_report(
        params,
        markdown,
        expected_sha256=digest,
        actor_id="dev-analyst",
        idempotency_key="manual-import-1",
    )
    # L'axe ne doit pas entrer dans l'idempotence du contenu.
    second, reused_second, _second_job_id = await service.import_standalone_report(
        parameters(axis="axe-renomme").model_copy(update={"edition_id": params.edition_id}),
        markdown,
        expected_sha256=digest,
        actor_id="dev-analyst",
        idempotency_key="manual-import-1",
    )

    assert reused_first is False
    assert reused_second is True
    assert second.id == first.id

    active = [
        batch for batch in await service.list_batches(params.edition_id) if batch.is_active_revision
    ]
    assert len(active) == 1


async def test_standalone_import_returns_reconciliation_job_id() -> None:
    """Régression : la consolidation en sujets tourne dans un job asynchrone.

    Le job id doit être renvoyé à l'appelant (§32.5 ne dispensait que du
    ModelRun/appel modèle préalable, pas de la réconciliation qui suit) pour
    qu'il puisse en attendre la fin avant de rafraîchir l'état, au lieu de
    faire la course avec ce job.
    """
    adapter = TransientResearchAdapter()
    gateway, _, _ = gateway_for_adapter(adapter)
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    expected_job_id = uuid4()

    class _StubJob:
        id = expected_job_id

    async def after_persisted_batch(
        batch: DiscoveryBatch, input_mode: object, actor_id: str
    ) -> _StubJob:
        del batch, input_mode, actor_id
        return _StubJob()

    service = DiscoveryService(
        discovery_uow,
        gateway,
        archive=gateway,
        bridge_capabilities_provider=FakeBridgeCapabilities(),
        after_persisted_batch=after_persisted_batch,
    )
    params = parameters(axis="manual-import")
    markdown = research_markdown_fixture()
    digest = (await service.preview_standalone_import(params, markdown))["sha256"]

    batch, reused, job_id = await service.import_standalone_report(
        params,
        markdown,
        expected_sha256=digest,
        actor_id="dev-analyst",
        idempotency_key="manual-import-1",
    )
    assert reused is False
    assert job_id == expected_job_id

    # Un réimport idempotent ne redéclenche pas de réconciliation : rien à
    # attendre de plus.
    _replay_batch, replay_reused, replay_job_id = await service.import_standalone_report(
        params,
        markdown,
        expected_sha256=digest,
        actor_id="dev-analyst",
        idempotency_key="manual-import-1",
    )
    assert replay_reused is True
    assert replay_job_id is None
    assert _replay_batch.id == batch.id


async def test_standalone_import_rejects_stale_confirmation() -> None:
    """§32.7 : une confirmation dont le contenu a changé ne persiste rien."""
    service, _, _ = _import_service()
    params = parameters(axis="manual-import")
    markdown = research_markdown_fixture()

    with pytest.raises(ValueError, match="no longer matches"):
        await service.import_standalone_report(
            params,
            markdown,
            expected_sha256="0" * 64,
            actor_id="dev-analyst",
            idempotency_key="manual-import-1",
        )

    assert await service.list_batches(params.edition_id) == []


async def test_reprocess_archived_report_revises_same_run_without_model_call() -> None:
    adapter = FakeModelAdapter(research_text=research_markdown_fixture())
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    params = parameters()
    run = DiscoveryRun(
        id=params.discovery_run_id,
        edition_id=params.edition_id,
        input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
        source_profile=params.source_profile,
        complementary_axis=params.complementary_axis,
        request_snapshot=discovery_request_snapshot(params),
        idempotency_key="reprocess-test-run",
        created_by="dev-analyst",
    )
    async with discovery_uow() as uow:
        assert await uow.discovery_runs.add_if_absent(run)
        await uow.commit()

    discovery = DiscoveryService(discovery_uow, gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    initial = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=run.id,
        idempotency_key=discovery_job_idempotency_key(run.id),
        correlation_id="initial-discovery",
        input_parameters=params.model_dump(mode="json"),
    )
    await dispatcher.dispatch(initial.id)
    batches_before = await discovery.list_batches(params.edition_id, include_replaced=True)
    assert len(batches_before) == 1
    initial_candidate_ids = {candidate.id for candidate in batches_before[0].candidates}
    assert set(discovery_uow.candidate_state) == initial_candidate_ids
    research_calls_before = len(adapter.calls)
    model_runs_before = len(model_uow.state)

    reprocess = await jobs.submit(
        kind=REPROCESS_DISCOVERY_REPORT_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=run.id,
        idempotency_key="reprocess-discovery-run:test",
        correlation_id="reprocess-discovery",
        input_parameters=ReprocessDiscoveryReportParameters(
            edition_id=run.edition_id,
            discovery_run_id=run.id,
            research_model_run_id=batches_before[0].discovery_model_run_id,
            actor_id="dev-analyst",
        ).model_dump(mode="json"),
    )
    await dispatcher.dispatch(reprocess.id)

    batches = await discovery.list_batches(params.edition_id, include_replaced=True)
    assert reprocess.id in job_uow.state
    assert len(discovery_uow.runs) == 1
    assert len(batches) == 2
    initial_batch, revision = sorted(batches, key=lambda batch: batch.parsing_revision)
    assert initial_batch.discovery_run_id == revision.discovery_run_id == run.id
    assert initial_batch.replaced_by_batch_id == revision.id
    assert revision.supersedes_batch_id == initial_batch.id
    assert revision.parsing_revision == 2
    assert len(discovery_uow.candidate_state) == 2
    assert initial_candidate_ids.isdisjoint({candidate.id for candidate in revision.candidates})
    active_candidates = await discovery.list_candidates_for_edition(params.edition_id)
    assert [item.discovery_batch_id for item in active_candidates] == [revision.id]
    assert [item.id for item in active_candidates] == [revision.candidates[0].id]
    assert len(model_uow.state) == model_runs_before
    assert len(adapter.calls) == research_calls_before


async def test_reprocess_existing_batch_retries_post_persisted_handoff_without_new_revision() -> (
    None
):
    """Le batch déterministe déjà persisté n'autorise pas à sauter le handoff.

    Le batch est committé avant le handoff cumulative : si le worker meurt
    entre les deux, le retry du MÊME Job retrouve ce batch. L'adopter en
    retournant immédiatement laissait le reprocess Job conclure sans intake ni
    réconciliation. Il doit au contraire rejouer le handoff, sans créer de N+2.
    """
    adapter = FakeModelAdapter(research_text=research_markdown_fixture())
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    discovery_uow = InMemoryDiscoveryUnitOfWorkFactory()
    params = parameters()
    run = DiscoveryRun(
        id=params.discovery_run_id,
        edition_id=params.edition_id,
        input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
        source_profile=params.source_profile,
        complementary_axis=params.complementary_axis,
        request_snapshot=discovery_request_snapshot(params),
        idempotency_key="reprocess-handoff-retry-run",
        created_by="dev-analyst",
    )
    async with discovery_uow() as uow:
        assert await uow.discovery_runs.add_if_absent(run)
        await uow.commit()

    initial_discovery = DiscoveryService(discovery_uow, gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, initial_discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    initial_job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=run.id,
        idempotency_key=discovery_job_idempotency_key(run.id),
        correlation_id="initial-discovery",
        input_parameters=params.model_dump(mode="json"),
    )
    await dispatcher.dispatch(initial_job.id)
    initial_batches = await initial_discovery.list_batches(params.edition_id, include_replaced=True)
    assert len(initial_batches) == 1
    initial_batch = initial_batches[0]
    initial_candidate_ids = {candidate.id for candidate in initial_batch.candidates}
    archived_model_run_id = initial_batch.discovery_model_run_id
    research_calls_before = len(adapter.calls)
    model_runs_before = len(model_uow.state)

    handoff_batch_ids: list[UUID] = []
    handoff_modes: list[object] = []
    handoff_fails = True

    async def after_persisted_batch(
        batch: DiscoveryBatch, input_mode: object, actor_id: str
    ) -> None:
        handoff_batch_ids.append(batch.id)
        handoff_modes.append(input_mode)
        if handoff_fails:
            raise RuntimeError("cumulative handoff lost with its worker")

    reprocessing = DiscoveryService(
        discovery_uow,
        gateway,
        archive=gateway,
        after_persisted_batch=after_persisted_batch,
    )
    reprocess_parameters = ReprocessDiscoveryReportParameters(
        edition_id=run.edition_id,
        discovery_run_id=run.id,
        research_model_run_id=archived_model_run_id,
        actor_id="dev-analyst",
    )
    reprocess_job = await jobs.submit(
        kind=REPROCESS_DISCOVERY_REPORT_JOB_KIND,
        aggregate_type="discovery_run",
        aggregate_id=run.id,
        idempotency_key="reprocess-discovery-run:handoff-retry",
        correlation_id="reprocess-handoff-retry",
        input_parameters=reprocess_parameters.model_dump(mode="json"),
        max_attempts=1,
    )

    async def claim_reprocess_job() -> JobExecutionContext:
        """Le même Job métier reprend son attempt : même id, même context."""
        async with job_uow() as uow:
            claimed = await uow.jobs.get_for_update(reprocess_job.id)
            assert claimed is not None
            if claimed.status is JobStatus.RUNNING:
                claimed.recover_abandoned(resume_current_attempt=True)
            claimed.start()
            await uow.jobs.save(claimed)
            await uow.commit()
        return JobExecutionContext(reprocess_job.id, job_uow)

    with pytest.raises(RuntimeError, match="cumulative handoff lost"):
        await reprocessing.reprocess_archived_report(
            reprocess_parameters, await claim_reprocess_job()
        )

    after_failure = sorted(
        await reprocessing.list_batches(params.edition_id, include_replaced=True),
        key=lambda batch: batch.parsing_revision,
    )
    assert len(after_failure) == 2
    revision = after_failure[1]
    assert after_failure[0].id == initial_batch.id
    assert after_failure[0].replaced_by_batch_id == revision.id
    assert revision.supersedes_batch_id == initial_batch.id
    assert revision.replaced_by_batch_id is None
    assert revision.parsing_revision == 2
    assert revision.discovery_run_id == run.id
    assert handoff_batch_ids == [revision.id]
    assert len(discovery_uow.candidate_state) == 2
    assert initial_candidate_ids.isdisjoint({candidate.id for candidate in revision.candidates})

    handoff_fails = False
    replayed = await reprocessing.reprocess_archived_report(
        reprocess_parameters, await claim_reprocess_job()
    )

    assert replayed.id == revision.id
    assert handoff_batch_ids == [revision.id, revision.id]
    assert handoff_modes == [DiscoveryInputMode.RECOVERY, DiscoveryInputMode.RECOVERY]
    after_replay = sorted(
        await reprocessing.list_batches(params.edition_id, include_replaced=True),
        key=lambda batch: batch.parsing_revision,
    )
    assert [batch.id for batch in after_replay] == [initial_batch.id, revision.id]
    assert after_replay[0].replaced_by_batch_id == revision.id
    assert after_replay[1].supersedes_batch_id == initial_batch.id
    assert after_replay[1].replaced_by_batch_id is None
    assert len(discovery_uow.runs) == 1
    assert len(discovery_uow.candidate_state) == 2
    assert len(model_uow.state) == model_runs_before
    assert len(adapter.calls) == research_calls_before

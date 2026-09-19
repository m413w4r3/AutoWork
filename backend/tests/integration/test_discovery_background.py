from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    ReprocessDiscoveryReportParameters,
    discovery_job_idempotency_key,
)
from cti_app.application.discovery.cumulative.jobs import (
    RECONCILE_DISCOVERY_JOB_KIND,
    ensure_discovery_reconciliation_job,
)
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.jobs import (
    DISCOVERY_JOB_KIND,
    REPROCESS_DISCOVERY_REPORT_JOB_KIND,
)
from cti_app.application.discovery.runs import DiscoveryRunService
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.editions import EditionService
from cti_app.application.jobs import JobExecutor, JobService, create_job_registry
from cti_app.application.model_gateway import ModelGateway, ModelRouter
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import DiscoveryBatch
from cti_app.domain.discovery_cumulative import DiscoveryInputMode
from cti_app.domain.jobs import Job, JobStatus
from cti_app.domain.model_runs import ModelBackend, ModelRunStatus
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.integrations.models import InMemoryModelOutputStore
from cti_app.workers.tasks import DURABLE_RESUME_JOB_KINDS
from tests.discovery_support import make_discovery_run_for_edition
from tests.test_discovery import DeferredResearchAdapter, research_markdown_fixture

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_job_lease_survives_long_background_bridge_run(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    adapter = DeferredResearchAdapter(pending_resumes=4)
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_backend=ModelBackend.FAKE,
        ),
        uow_factory,
        output_store,
    )
    edition_service = EditionService(uow_factory)
    discovery_uow = uow_factory
    recovery_passes: list[list[object]] = []
    heartbeats: list[datetime] = []
    try:
        edition = await edition_service.create(
            country="Durable Background Test",
            country_code="DB",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr", "en", "fa"),
            actor_id="integration-analyst",
            correlation_id="durable-discovery-integration",
        )
        discovery_run = await make_discovery_run_for_edition(
            uow_factory,
            edition,
            source_profile="iran-default",
            complementary_axis="campagnes techniques",
        )
        parameters = DiscoverEditionParameters(
            edition_id=edition.id,
            discovery_run_id=discovery_run.id,
            country=edition.country,
            country_code=edition.country_code,
            country_aliases=["DB"],
            period_start=edition.period_start,
            period_end=edition.period_end,
            languages=list(edition.languages),
            source_profile="iran-default",
            keywords=["APT", "IOC"],
            exclusions=[],
            complementary_axis="campagnes techniques",
            tlp=edition.tlp,
            sensitivity="internal",
            external_llm_allowed=True,
        )

        async def poll_cycle(_: float) -> None:
            async with uow_factory() as uow:
                current = await uow.jobs.get_for_update(job.id)
                assert current is not None
                current.started_at = datetime.now(UTC) - timedelta(minutes=10)
                assert current.heartbeat_at is not None
                heartbeats.append(current.heartbeat_at)
                await uow.jobs.save(current)
                await uow.commit()
            recovery_passes.append(list(await jobs.recover_abandoned(timedelta(seconds=120))))

        discovery = DiscoveryService(
            discovery_uow,
            gateway,
            archive=gateway,
            background_poll_interval_seconds=5,
            background_waiter=poll_cycle,
        )
        registry = create_job_registry(gateway, discovery)
        jobs = JobService(uow_factory, registry)
        executor = JobExecutor(uow_factory, registry)
        job = await jobs.submit(
            kind=DISCOVERY_JOB_KIND,
            aggregate_type="discovery_run",
            aggregate_id=discovery_run.id,
            idempotency_key=discovery_job_idempotency_key(discovery_run.id),
            correlation_id="durable-discovery-integration",
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=1,
        )

        completed = await executor.execute(job.id)

        assert completed.status is JobStatus.SUCCEEDED
        assert completed.progress_current == completed.progress_total == 4
        assert adapter.resume_calls == 5
        assert len(adapter.calls) == 1
        assert recovery_passes == [[], [], [], []]
        assert heartbeats == sorted(heartbeats)
        batches = await discovery.list_batches(edition.id)
        assert len(batches) == 1
        assert len(batches[0].candidates) == 1
        async with uow_factory() as uow:
            run = await uow.model_runs.get(batches[0].discovery_model_run_id)
        assert run is not None
        assert run.status is ModelRunStatus.SUCCEEDED
        assert run.response_id is not None
        archived = await output_store.read(run.output_references[0], max_bytes=10_000_000)
        assert archived.decode() == research_markdown_fixture()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_concurrent_reprocess_jobs_form_one_linear_revision_chain(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    adapter = DeferredResearchAdapter()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_backend=ModelBackend.FAKE,
        ),
        uow_factory,
        output_store,
    )
    edition_service = EditionService(uow_factory)
    try:
        edition = await edition_service.create(
            country="Concurrent Reprocess Test",
            country_code="CR",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr", "en"),
            actor_id="integration-analyst",
            correlation_id="concurrent-reprocess-integration",
        )
        discovery_run = await make_discovery_run_for_edition(
            uow_factory,
            edition,
            source_profile="iran-default",
            complementary_axis="campagnes techniques",
        )
        parameters = DiscoverEditionParameters(
            edition_id=edition.id,
            discovery_run_id=discovery_run.id,
            country=edition.country,
            country_code=edition.country_code,
            country_aliases=["CR"],
            period_start=edition.period_start,
            period_end=edition.period_end,
            languages=list(edition.languages),
            source_profile="iran-default",
            keywords=["APT"],
            exclusions=[],
            complementary_axis="campagnes techniques",
            tlp=edition.tlp,
            sensitivity="internal",
            external_llm_allowed=True,
        )
        discovery = DiscoveryService(uow_factory, gateway, archive=gateway)
        registry = create_job_registry(gateway, discovery)
        jobs = JobService(uow_factory, registry)
        executor = JobExecutor(uow_factory, registry)

        initial_job = await jobs.submit(
            kind=DISCOVERY_JOB_KIND,
            aggregate_type="discovery_run",
            aggregate_id=discovery_run.id,
            idempotency_key=discovery_job_idempotency_key(discovery_run.id),
            correlation_id="concurrent-reprocess-initial",
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=1,
        )
        initial = await executor.execute(initial_job.id)
        assert initial.status is JobStatus.SUCCEEDED
        initial_batches = await discovery.list_batches(edition.id, include_replaced=True)
        assert len(initial_batches) == 1
        research_model_run_id = initial_batches[0].discovery_model_run_id

        reprocess_parameters = ReprocessDiscoveryReportParameters(
            edition_id=edition.id,
            discovery_run_id=discovery_run.id,
            research_model_run_id=research_model_run_id,
            actor_id="integration-analyst",
        ).model_dump(mode="json")
        first_job = await jobs.submit(
            kind=REPROCESS_DISCOVERY_REPORT_JOB_KIND,
            aggregate_type="discovery_run",
            aggregate_id=discovery_run.id,
            idempotency_key="concurrent-reprocess-1",
            correlation_id="concurrent-reprocess-1",
            input_parameters=reprocess_parameters,
            max_attempts=1,
        )
        second_job = await jobs.submit(
            kind=REPROCESS_DISCOVERY_REPORT_JOB_KIND,
            aggregate_type="discovery_run",
            aggregate_id=discovery_run.id,
            idempotency_key="concurrent-reprocess-2",
            correlation_id="concurrent-reprocess-2",
            input_parameters=reprocess_parameters,
            max_attempts=1,
        )

        completed_first, completed_second = await asyncio.gather(
            executor.execute(first_job.id), executor.execute(second_job.id)
        )

        assert completed_first.status is JobStatus.SUCCEEDED
        assert completed_second.status is JobStatus.SUCCEEDED
        batches = sorted(
            await discovery.list_batches(edition.id, include_replaced=True),
            key=lambda batch: batch.parsing_revision,
        )
        assert len(batches) == 3
        assert [batch.parsing_revision for batch in batches] == [1, 2, 3]
        assert {batch.discovery_run_id for batch in batches} == {discovery_run.id}
        assert batches[0].supersedes_batch_id is None
        assert batches[0].replaced_by_batch_id == batches[1].id
        assert batches[1].replaced_by_batch_id == batches[2].id
        assert batches[2].replaced_by_batch_id is None
        assert batches[1].supersedes_batch_id == batches[0].id
        assert batches[2].supersedes_batch_id == batches[1].id
        assert {batch.supersedes_batch_id for batch in batches[1:]} == {
            batches[0].id,
            batches[1].id,
        }
        assert {batch.replaced_by_batch_id for batch in batches[:-1]} == {
            batches[1].id,
            batches[2].id,
        }
    finally:
        await engine.dispose()


class _SimulatedWorkerLoss(BaseException):
    """Perte brutale du worker : hors de la hiérarchie `Exception`, donc le
    JobExecutor ne la convertit pas en échec et le Job reste RUNNING, comme
    après un SIGKILL."""


class _WorkerLossDispatcher:
    """Exécuteur synchrone qui peut perdre un dispatch déjà committé."""

    def __init__(self, executor: JobExecutor) -> None:
        self._executor = executor
        self.attempts: list[UUID] = []
        self.lost: list[UUID] = []
        self.lose_next_dispatch = False

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        self.attempts.append(job_id)
        if self.lose_next_dispatch:
            self.lose_next_dispatch = False
            self.lost.append(job_id)
            raise _SimulatedWorkerLoss(str(job_id))
        await self._executor.execute(job_id)


@pytest.mark.asyncio
async def test_postgres_reprocess_worker_loss_replays_cumulative_handoff_after_dispatch_failure(
    migrated_postgres_url: str,
) -> None:
    """Le handoff cumulative d'un reprocess doit survivre à une perte de worker.

    Le batch révisé est committé avant son handoff. Si le Job enfant de
    réconciliation est committé puis son dispatch perdu, reprendre le MÊME
    reprocess Job doit adopter le même batch, le même intake et le même Job
    enfant, puis le redispatcher — sans créer de N+2 ni de second enfant.
    """
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    adapter = DeferredResearchAdapter()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_backend=ModelBackend.FAKE,
        ),
        uow_factory,
        output_store,
    )
    edition_service = EditionService(uow_factory)
    try:
        edition = await edition_service.create(
            country="Reprocess Handoff Test",
            country_code="RH",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr", "en"),
            actor_id="integration-analyst",
            correlation_id="reprocess-handoff-integration",
        )
        discovery_run = await make_discovery_run_for_edition(
            uow_factory,
            edition,
            source_profile="iran-default",
            complementary_axis="campagnes techniques",
        )
        parameters = DiscoverEditionParameters(
            edition_id=edition.id,
            discovery_run_id=discovery_run.id,
            country=edition.country,
            country_code=edition.country_code,
            country_aliases=["RH"],
            period_start=edition.period_start,
            period_end=edition.period_end,
            languages=list(edition.languages),
            source_profile="iran-default",
            keywords=["APT"],
            exclusions=[],
            complementary_axis="campagnes techniques",
            tlp=edition.tlp,
            sensitivity="internal",
            external_llm_allowed=True,
        )

        cumulative = CumulativeDiscoveryService(uow_factory)
        jobs: JobService
        dispatcher: _WorkerLossDispatcher

        async def handoff(batch: object, input_mode: object, actor_id: str) -> object:
            assert isinstance(batch, DiscoveryBatch)
            assert isinstance(input_mode, DiscoveryInputMode)
            return await ensure_discovery_reconciliation_job(
                batch,
                input_mode=input_mode,
                actor_id=actor_id,
                cumulative_discovery_service=cumulative,
                job_service=jobs,
                job_dispatcher=dispatcher,
                correlation_id="reprocess-handoff-integration",
            )

        discovery = DiscoveryService(
            uow_factory,
            gateway,
            archive=gateway,
            after_persisted_batch=handoff,
        )
        registry = create_job_registry(gateway, discovery, cumulative_discovery_service=cumulative)
        jobs = JobService(uow_factory, registry)
        executor = JobExecutor(uow_factory, registry)
        dispatcher = _WorkerLossDispatcher(executor)
        runs = DiscoveryRunService(uow_factory, jobs, dispatcher)

        initial_job = await jobs.submit(
            kind=DISCOVERY_JOB_KIND,
            aggregate_type="discovery_run",
            aggregate_id=discovery_run.id,
            idempotency_key=discovery_job_idempotency_key(discovery_run.id),
            correlation_id="reprocess-handoff-initial",
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=1,
        )
        initial = await executor.execute(initial_job.id)
        assert initial.status is JobStatus.SUCCEEDED
        initial_batches = await discovery.list_batches(edition.id, include_replaced=True)
        assert len(initial_batches) == 1
        initial_batch = initial_batches[0]
        research_model_run_id = initial_batch.discovery_model_run_id

        reprocess_parameters = ReprocessDiscoveryReportParameters(
            edition_id=edition.id,
            discovery_run_id=discovery_run.id,
            research_model_run_id=research_model_run_id,
            actor_id="integration-analyst",
        )
        reprocess_job = await jobs.submit(
            kind=REPROCESS_DISCOVERY_REPORT_JOB_KIND,
            aggregate_type="discovery_run",
            aggregate_id=discovery_run.id,
            idempotency_key=f"reprocess-discovery-run:{discovery_run.id}:worker-loss",
            correlation_id="reprocess-handoff-integration",
            input_parameters=reprocess_parameters.model_dump(mode="json"),
            max_attempts=1,
        )

        # Le Job enfant est committé par `submit`, puis son dispatch est perdu.
        dispatcher.lose_next_dispatch = True
        with pytest.raises(_SimulatedWorkerLoss):
            await executor.execute(reprocess_job.id)

        interrupted = await jobs.get(reprocess_job.id)
        assert interrupted.status is JobStatus.RUNNING
        assert interrupted.attempt == 1

        after_loss = sorted(
            await discovery.list_batches(edition.id, include_replaced=True),
            key=lambda batch: batch.parsing_revision,
        )
        assert [batch.parsing_revision for batch in after_loss] == [1, 2]
        revision = after_loss[1]
        async with uow_factory() as uow:
            intake = await uow.discovery_intakes.get_by_batch(revision.id)
        assert intake is not None
        child_key = f"reconcile-discovery:{intake.id}"
        async with uow_factory() as uow:
            lost_child = await uow.jobs.get_by_idempotency_key(child_key)
        assert lost_child is not None
        assert lost_child.status is JobStatus.QUEUED
        assert lost_child.next_retry_at is None
        assert dispatcher.lost == [lost_child.id]

        # Perte de worker : le heartbeat expire et le kind durable de production
        # fait reprendre le MÊME attempt métier, donc le même context.job_id.
        async with uow_factory() as uow:
            stale = await uow.jobs.get_for_update(reprocess_job.id)
            assert stale is not None
            stale.heartbeat_at = datetime.now(UTC) - timedelta(minutes=30)
            await uow.jobs.save(stale)
            await uow.commit()
        assert REPROCESS_DISCOVERY_REPORT_JOB_KIND in DURABLE_RESUME_JOB_KINDS
        recovered = await jobs.recover_abandoned(
            timedelta(seconds=120),
            resume_current_attempt_kinds=DURABLE_RESUME_JOB_KINDS,
        )
        assert [job.id for job in recovered] == [reprocess_job.id]
        assert recovered[0].status is JobStatus.QUEUED
        assert recovered[0].attempt == 0

        resumed = await executor.execute(reprocess_job.id)

        assert resumed.id == reprocess_job.id
        assert resumed.status is JobStatus.SUCCEEDED
        assert resumed.output_reference == f"discovery-batch://{revision.id}"

        final_batches = sorted(
            await discovery.list_batches(edition.id, include_replaced=True),
            key=lambda batch: batch.parsing_revision,
        )
        assert [batch.id for batch in final_batches] == [initial_batch.id, revision.id]
        assert final_batches[0].replaced_by_batch_id == revision.id
        assert final_batches[1].supersedes_batch_id == initial_batch.id
        assert final_batches[1].replaced_by_batch_id is None

        async with uow_factory() as uow:
            intakes = list(await uow.discovery_intakes.list_for_edition(edition.id))
            replayed_intake = await uow.discovery_intakes.get_by_batch(revision.id)
            snapshot = await uow.discovery_snapshots.get_for_intake(intake.id)
            children = [
                job
                for job in await uow.jobs.list_for_aggregate("edition", edition.id)
                if job.kind == RECONCILE_DISCOVERY_JOB_KIND and job.idempotency_key == child_key
            ]
        assert replayed_intake is not None
        assert replayed_intake.id == intake.id
        assert [intake_row.batch_id for intake_row in intakes].count(revision.id) == 1
        assert len(children) == 1
        assert children[0].id == lost_child.id
        assert children[0].status is JobStatus.SUCCEEDED
        assert snapshot is not None
        assert snapshot.intake_id == intake.id
        assert dispatcher.attempts.count(lost_child.id) == 2

        projection = await runs.get(discovery_run.id)
        assert projection.result is not None
        assert projection.result.id == revision.id
        async with uow_factory() as uow:
            active = await uow.discovery_snapshots.get_active(edition.id)
        assert active is not None
        assert active.id == snapshot.id
        assert active.intake_id == intake.id
    finally:
        await engine.dispose()


class _RecordingDispatcher:
    """Enregistre les dispatchs sans rien exécuter."""

    def __init__(self) -> None:
        self.attempts: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        self.attempts.append(job_id)


@pytest.mark.asyncio
async def test_postgres_reconciliation_handoff_never_preempts_a_scheduled_retry(
    migrated_postgres_url: str,
) -> None:
    """Rejouer le handoff ne doit pas court-circuiter un backoff en cours.

    Le redispatch d'un enfant dupliqué ne vise que le cas « committé puis
    dispatch perdu ». Un enfant QUEUED avec `next_retry_at` appartient au
    mécanisme de retry, qui reste autoritaire sur sa date d'exécution.
    """
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    adapter = DeferredResearchAdapter()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_backend=ModelBackend.FAKE,
        ),
        uow_factory,
        output_store,
    )
    edition_service = EditionService(uow_factory)
    try:
        edition = await edition_service.create(
            country="Reconciliation Backoff Test",
            country_code="RK",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr", "en"),
            actor_id="integration-analyst",
            correlation_id="reconciliation-backoff-integration",
        )
        discovery_run = await make_discovery_run_for_edition(
            uow_factory,
            edition,
            source_profile="iran-default",
            complementary_axis="campagnes techniques",
        )
        parameters = DiscoverEditionParameters(
            edition_id=edition.id,
            discovery_run_id=discovery_run.id,
            country=edition.country,
            country_code=edition.country_code,
            country_aliases=["RK"],
            period_start=edition.period_start,
            period_end=edition.period_end,
            languages=list(edition.languages),
            source_profile="iran-default",
            keywords=["APT"],
            exclusions=[],
            complementary_axis="campagnes techniques",
            tlp=edition.tlp,
            sensitivity="internal",
            external_llm_allowed=True,
        )

        cumulative = CumulativeDiscoveryService(uow_factory)
        jobs: JobService
        dispatcher = _RecordingDispatcher()

        async def handoff(batch: object, input_mode: object, actor_id: str) -> object:
            assert isinstance(batch, DiscoveryBatch)
            assert isinstance(input_mode, DiscoveryInputMode)
            return await ensure_discovery_reconciliation_job(
                batch,
                input_mode=input_mode,
                actor_id=actor_id,
                cumulative_discovery_service=cumulative,
                job_service=jobs,
                job_dispatcher=dispatcher,
                correlation_id="reconciliation-backoff-integration",
            )

        discovery = DiscoveryService(
            uow_factory,
            gateway,
            archive=gateway,
            after_persisted_batch=handoff,
        )
        registry = create_job_registry(gateway, discovery, cumulative_discovery_service=cumulative)
        jobs = JobService(uow_factory, registry)
        executor = JobExecutor(uow_factory, registry)

        initial_job = await jobs.submit(
            kind=DISCOVERY_JOB_KIND,
            aggregate_type="discovery_run",
            aggregate_id=discovery_run.id,
            idempotency_key=discovery_job_idempotency_key(discovery_run.id),
            correlation_id="reconciliation-backoff-initial",
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=1,
        )
        assert (await executor.execute(initial_job.id)).status is JobStatus.SUCCEEDED
        batches = await discovery.list_batches(edition.id, include_replaced=True)
        assert len(batches) == 1
        batch = batches[0]
        async with uow_factory() as uow:
            intake = await uow.discovery_intakes.get_by_batch(batch.id)
        assert intake is not None
        async with uow_factory() as uow:
            child = await uow.jobs.get_by_idempotency_key(f"reconcile-discovery:{intake.id}")
        assert child is not None
        assert dispatcher.attempts == [child.id]

        # Retry temporisé en cours : le handoff rejoué doit le laisser tranquille.
        async with uow_factory() as uow:
            scheduled = await uow.jobs.get_for_update(child.id)
            assert scheduled is not None
            scheduled.next_retry_at = datetime.now(UTC) + timedelta(minutes=5)
            await uow.jobs.save(scheduled)
            await uow.commit()

        backed_off = await handoff(batch, DiscoveryInputMode.BRIDGE_RESEARCH, "system:discovery")
        assert isinstance(backed_off, Job)
        assert backed_off.id == child.id
        assert dispatcher.attempts == [child.id]

        # Committé mais jamais dispatché : là, et seulement là, on redispatche.
        async with uow_factory() as uow:
            undispatched = await uow.jobs.get_for_update(child.id)
            assert undispatched is not None
            undispatched.next_retry_at = None
            await uow.jobs.save(undispatched)
            await uow.commit()

        recovered = await handoff(batch, DiscoveryInputMode.BRIDGE_RESEARCH, "system:discovery")
        assert isinstance(recovered, Job)
        assert recovered.id == child.id
        assert dispatcher.attempts == [child.id, child.id]

        async with uow_factory() as uow:
            intakes = list(await uow.discovery_intakes.list_for_edition(edition.id))
            children = [
                job
                for job in await uow.jobs.list_for_aggregate("edition", edition.id)
                if job.kind == RECONCILE_DISCOVERY_JOB_KIND
            ]
        assert len(intakes) == 1
        assert len(children) == 1
    finally:
        await engine.dispose()

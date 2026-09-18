from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    ReprocessDiscoveryReportParameters,
    discovery_job_idempotency_key,
)
from cti_app.application.discovery.jobs import (
    DISCOVERY_JOB_KIND,
    REPROCESS_DISCOVERY_REPORT_JOB_KIND,
)
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.editions import EditionService
from cti_app.application.jobs import JobExecutor, JobService, create_job_registry
from cti_app.application.model_gateway import ModelGateway, ModelRouter
from cti_app.domain.classification import TLP
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import ModelBackend, ModelRunStatus
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.integrations.models import InMemoryModelOutputStore
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

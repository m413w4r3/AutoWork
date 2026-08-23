from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    discovery_idempotency_key,
)
from cti_app.application.discovery.jobs import DISCOVERY_JOB_KIND
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.editions import EditionService
from cti_app.application.jobs import JobExecutor, JobService, create_job_registry
from cti_app.application.model_gateway import ModelGateway, ModelRouter
from cti_app.domain.classification import TLP
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import ModelProvider, ModelRunStatus
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.integrations.models import InMemoryModelOutputStore
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
            forced_provider=ModelProvider.FAKE,
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
            target_major_articles=2,
            target_briefs=6,
            previous_edition_id=None,
            source_profile="iran-default",
            actor_id="integration-analyst",
            correlation_id="durable-discovery-integration",
        )
        parameters = DiscoverEditionParameters(
            edition_id=edition.id,
            country=edition.country,
            country_aliases=["DB"],
            period_start=edition.period_start,
            period_end=edition.period_end,
            languages=list(edition.languages),
            source_profile=edition.source_profile,
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
            aggregate_type="edition",
            aggregate_id=edition.id,
            idempotency_key=discovery_idempotency_key(parameters),
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

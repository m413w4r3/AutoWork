import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import dramatiq

from cti_app.application.discovery import DiscoveryService
from cti_app.application.editorial import EditorialGroupingService
from cti_app.application.jobs import JobExecutor, JobService, create_job_registry
from cti_app.application.persistence import JobUnitOfWork, UnitOfWork
from cti_app.config import get_settings
from cti_app.domain.jobs import JobStatus
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.integrations.model_factory import (
    create_bridge_capabilities_provider,
    create_model_gateway,
)
from cti_app.workers.broker import broker as broker


@dramatiq.actor(max_retries=0)
def worker_probe() -> None:
    """No-op actor proving worker wiring; it is not a CTI business task."""


@dramatiq.actor(max_retries=0)
def execute_job(job_id: str) -> None:
    """Execute one canonical job; application code owns retry decisions."""
    delay_ms = asyncio.run(_execute_job(UUID(job_id)))
    if delay_ms is not None:
        execute_job.send_with_options(args=(job_id,), delay=delay_ms)


@dramatiq.actor(max_retries=0)
def recover_abandoned_jobs() -> None:
    """Requeue jobs whose running worker stopped updating its heartbeat."""
    for job_id in asyncio.run(_recover_abandoned_jobs()):
        execute_job.send(str(job_id))


async def _execute_job(job_id: UUID) -> int | None:
    settings = get_settings()
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    try:
        model_gateway = create_model_gateway(settings, uow_factory)
        editorial_service = EditorialGroupingService(uow_factory, model_gateway)
        discovery_service = DiscoveryService(
            uow_factory,
            model_gateway,
            model_gateway,
            bridge_capabilities_provider=create_bridge_capabilities_provider(settings),
            after_discovery=editorial_service.synchronize,
        )
        executor = JobExecutor(
            uow_factory,
            create_job_registry(model_gateway, discovery_service),
            retry_base_seconds=settings.job_retry_base_seconds,
            retry_max_seconds=settings.job_retry_max_seconds,
        )
        job = await executor.execute(job_id)
        if job.status is JobStatus.QUEUED and job.next_retry_at is not None:
            seconds = max(0.0, (job.next_retry_at - datetime.now(UTC)).total_seconds())
            return int(seconds * 1000)
        return None
    finally:
        await engine.dispose()


async def _recover_abandoned_jobs() -> list[UUID]:
    settings = get_settings()
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> JobUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    try:
        service = JobService(uow_factory, create_job_registry())
        jobs = await service.recover_abandoned(
            timedelta(seconds=settings.job_heartbeat_timeout_seconds)
        )
        return [job.id for job in jobs if job.status is JobStatus.QUEUED]
    finally:
        await engine.dispose()

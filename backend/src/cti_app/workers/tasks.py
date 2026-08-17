import asyncio
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import UUID

import dramatiq
from minio import Minio

from cti_app.application.briefs import BriefService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.discovery import DiscoveryService
from cti_app.application.editorial import EditorialGroupingService
from cti_app.application.http_collection import (
    CollectionPolicy,
    SafeHttpCollector,
    SystemDnsResolver,
    parse_domain_policy,
)
from cti_app.application.jobs import JobExecutor, JobService, create_job_registry
from cti_app.application.persistence import JobUnitOfWork, UnitOfWork
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.config import get_settings
from cti_app.domain.jobs import JobStatus
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.http import AsyncioPinnedHttpTransport
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
            after_discovery=partial(editorial_service.synchronize, resolve_ambiguous=False),
            background_poll_interval_seconds=settings.discovery_bridge_poll_interval_seconds,
        )
        blob_store = MinioBlobStore(
            Minio(
                settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                secure=settings.s3_secure,
            ),
            physical_bucket=settings.s3_bucket,
        )
        collection_service = SubjectCollectionService(
            uow_factory,
            SafeHttpCollector(
                AsyncioPinnedHttpTransport(),
                SystemDnsResolver(),
                CollectionPolicy(
                    max_redirects=settings.collection_max_redirects,
                    timeout_seconds=settings.collection_timeout_seconds,
                    max_download_bytes=settings.collection_max_download_bytes,
                    max_expanded_bytes=settings.collection_max_expanded_bytes,
                    max_decompression_ratio=settings.collection_max_decompression_ratio,
                    allowed_domains=parse_domain_policy(settings.collection_allowed_domains),
                    blocked_domains=parse_domain_policy(settings.collection_blocked_domains),
                ),
            ),
            blob_store,
            fetch_lease_seconds=settings.collection_fetch_lease_seconds,
            workspace_materializer=SubjectWorkspaceMaterializer(blob_store),
            workspace_root=settings.subject_workspace_root,
        )
        brief_service = BriefService(uow_factory, blob_store, model_gateway)
        executor = JobExecutor(
            uow_factory,
            create_job_registry(
                model_gateway, discovery_service, collection_service, brief_service
            ),
            retry_base_seconds=settings.job_retry_base_seconds,
            retry_max_seconds=settings.job_retry_max_seconds,
            heartbeat_interval_seconds=min(20.0, settings.job_heartbeat_timeout_seconds / 3),
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

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import dramatiq
from minio import Minio

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.jobs import JobExecutor
from cti_app.application.static_analysis import StaticAnalysisService, create_analysis_job_registry
from cti_app.config import get_settings
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.workers.broker import broker as broker


@dramatiq.actor(queue_name="analysis", max_retries=0)
def execute_analysis_job(job_id: str) -> None:
    asyncio.run(_execute_analysis_job(UUID(job_id)))


async def _execute_analysis_job(job_id: UUID) -> None:
    settings = get_settings()
    engine = create_postgres_engine(settings.postgres_dsn)
    sessions = create_session_factory(engine)

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(sessions)

    try:
        store = MinioBlobStore(
            Minio(
                settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                secure=settings.s3_secure,
            ),
            physical_bucket=settings.s3_bucket,
        )
        blobs = BlobCatalogService(store, factory)
        service = StaticAnalysisService(
            blobs,
            factory,
            max_sample_bytes=settings.analysis_max_sample_bytes,
            string_min_length=settings.analysis_string_min_length,
            max_strings=settings.analysis_max_strings,
        )
        capabilities_service = CapabilitiesService(
            blobs,
            factory,
            rules_path=settings.analysis_capa_rules_path,
            timeout_seconds=settings.analysis_capa_timeout_seconds,
            max_output_bytes=settings.analysis_capa_max_output_bytes,
            max_memory_bytes=settings.analysis_capa_max_memory_bytes,
        )
        executor = JobExecutor(
            factory,
            create_analysis_job_registry(service, capabilities_service),
            retry_base_seconds=settings.job_retry_base_seconds,
            retry_max_seconds=settings.job_retry_max_seconds,
        )
        result = await executor.execute(job_id)
        if (
            result is not None
            and getattr(getattr(result, "status", None), "value", None) == "QUEUED"
            and result.next_retry_at is not None
        ):
            delay_ms = max(
                0,
                int((result.next_retry_at - datetime.now(UTC)).total_seconds() * 1000),
            )
            execute_analysis_job.send_with_options(args=(str(job_id),), delay=delay_ms)
    finally:
        await engine.dispose()

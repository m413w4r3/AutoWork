import asyncio
import json
import re
from datetime import UTC, datetime
from uuid import UUID

import dramatiq
from minio import Minio

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.confined_analysis import (
    CONFINED_ANALYSIS_JOB_KIND,
    ConfinedAnalysisJobParameters,
)
from cti_app.application.jobs import (
    JobExecutionContext,
    JobExecutor,
    JobHandler,
    JobParameters,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.static_analysis import StaticAnalysisService, create_analysis_job_registry
from cti_app.config import Settings, get_settings
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.workers.broker import broker as broker
from cti_app.workers.confined_analysis import (
    build_analysis_worker_probe_payload,
    execute_confined_analysis_job,
)

_PROBE_ID = re.compile(r"^[0-9a-f]{32}$")
_PROBE_KEY_PREFIX = "autowork:analysis-worker-probe:"


@dramatiq.actor(queue_name="analysis", max_retries=0)
def execute_analysis_job(job_id: str) -> None:
    asyncio.run(_execute_analysis_job(UUID(job_id)))


@dramatiq.actor(queue_name="analysis-probe", max_retries=0)
def probe_analysis_worker(probe_id: str) -> None:
    """Publish only non-secret runtime versions and the enforced isolation profile."""
    if _PROBE_ID.fullmatch(probe_id) is None:
        return
    try:
        settings = get_settings()
        payload = build_analysis_worker_probe_payload(settings)
        payload.update(asyncio.run(_probe_worker_dependencies(settings)))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        broker.client.setex(f"{_PROBE_KEY_PREFIX}{probe_id}", 15, encoded)
    except Exception:
        # A missing reply is treated as an unavailable worker by the caller.
        return


def build_confined_analysis_job_handler(
    *,
    blobs: BlobCatalogService,
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
) -> JobHandler:
    """Build the analysis_tasks handler for one persisted confined-analysis job."""

    async def handle(parameters: JobParameters, _context: JobExecutionContext) -> str:
        if not isinstance(parameters, ConfinedAnalysisJobParameters):
            raise TypeError("invalid confined analysis parameters")
        return await execute_confined_analysis_job(
            parameters,
            blobs=blobs,
            uow_factory=uow_factory,
            settings=settings,
        )

    return handle


async def _probe_worker_dependencies(settings: Settings) -> dict[str, bool]:
    """Confirm the isolated service can reach its required canonical stores."""
    postgres_access = False
    minio_access = False
    try:
        async with asyncio.timeout(settings.analysis_worker_probe_timeout_seconds):
            engine = create_postgres_engine(settings.postgres_dsn)
            try:
                async with engine.connect() as connection:
                    await connection.exec_driver_sql("SELECT 1")
                postgres_access = True
            finally:
                await engine.dispose()
    except Exception:
        postgres_access = False
    try:

        def check_bucket() -> bool:
            client = Minio(
                settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                secure=settings.s3_secure,
            )
            return client.bucket_exists(settings.s3_bucket)

        async with asyncio.timeout(settings.analysis_worker_probe_timeout_seconds):
            minio_access = await asyncio.to_thread(check_bucket)
    except Exception:
        minio_access = False
    return {"postgres_access": postgres_access, "minio_access": minio_access}


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
        registry = create_analysis_job_registry(service, capabilities_service)

        registry.register(
            CONFINED_ANALYSIS_JOB_KIND,
            ConfinedAnalysisJobParameters,
            build_confined_analysis_job_handler(
                blobs=blobs,
                uow_factory=factory,
                settings=settings,
            ),
        )
        executor = JobExecutor(
            factory,
            registry,
            retry_base_seconds=settings.job_retry_base_seconds,
            retry_max_seconds=settings.job_retry_max_seconds,
            bridge_ui_retry_base_seconds=settings.job_bridge_ui_retry_base_seconds,
            bridge_ui_retry_max_seconds=settings.job_bridge_ui_retry_max_seconds,
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

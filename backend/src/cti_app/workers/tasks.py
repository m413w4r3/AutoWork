import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import dramatiq
import httpx
from minio import Minio

from cti_app.application.analyst_vt_enrichment import VirusTotalSeedEnrichmentService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.briefs import BriefService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.discovery.cumulative.chatgpt_planner import ChatGptMergePlanner
from cti_app.application.discovery.cumulative.contracts import ReconcileDiscoveryParameters
from cti_app.application.discovery.cumulative.jobs import RECONCILE_DISCOVERY_JOB_KIND
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.jobs import DISCOVERY_JOB_KIND
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.editorial import EditorialGroupingService
from cti_app.application.http_collection import (
    CollectionPolicy,
    SafeHttpCollector,
    SystemDnsResolver,
    parse_domain_policy,
)
from cti_app.application.jobs import (
    DuplicateJobError,
    JobExecutor,
    JobService,
    create_job_registry,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import JobUnitOfWork, UnitOfWork
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_jobs import ProductionStageChain
from cti_app.application.virustotal import VirusTotalCapabilities, VirusTotalRoutingPolicy
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.config import get_settings
from cti_app.domain.jobs import JobStatus
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.http import AsyncioPinnedHttpTransport
from cti_app.infrastructure.jobs import DramatiqJobDispatcher
from cti_app.infrastructure.virustotal import (
    VirusTotalHttpAdapter,
    create_virustotal_direct_http_client,
    create_virustotal_http_client,
)
from cti_app.integrations.model_factory import (
    create_bridge_capabilities_provider,
    create_model_gateway,
)
from cti_app.logging import get_correlation_id
from cti_app.workers.broker import broker as broker

# Une recherche ChatGPT durable dépasse largement la limite Dramatiq par
# défaut de 600 000 ms, qui tuait le worker en pleine attente du bridge.
EXECUTE_JOB_TIME_LIMIT_MS = int(get_settings().job_actor_time_limit_seconds * 1000)

# Kinds dont l'attente survit à la perte du worker. Le processus de recovery
# les déclare explicitement pour ne pas construire tous les services métier.
DURABLE_RESUME_JOB_KINDS = frozenset({DISCOVERY_JOB_KIND})


@dramatiq.actor(max_retries=0)
def worker_probe() -> None:
    """No-op actor proving worker wiring; it is not a CTI business task."""


@dramatiq.actor(max_retries=0, time_limit=EXECUTE_JOB_TIME_LIMIT_MS)
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
    vt_proxy_client: httpx.AsyncClient | None = None
    vt_direct_client: httpx.AsyncClient | None = None

    def uow_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    try:
        if settings.virustotal_proxy_url:
            vt_proxy_client = create_virustotal_http_client(settings)
        else:
            vt_proxy_client = httpx.AsyncClient()
        if settings.virustotal_api_key and settings.virustotal_file_report_legacy_fallback_enabled:
            vt_direct_client = create_virustotal_direct_http_client(settings)
        vt_adapter = VirusTotalHttpAdapter(
            client=vt_proxy_client,
            base_url=settings.virustotal_base_url,
            fallback_base_url=settings.virustotal_fallback_base_url,
            legacy_base_url=settings.virustotal_legacy_base_url,
            direct_client=vt_direct_client,
            api_key=(
                settings.virustotal_api_key.get_secret_value()
                if vt_direct_client is not None and settings.virustotal_api_key is not None
                else None
            ),
            capabilities=VirusTotalCapabilities(
                file_report=settings.virustotal_file_report_enabled
            ),
            file_report_proxy_fallback_enabled=(
                settings.virustotal_file_report_proxy_fallback_enabled
            ),
            file_report_legacy_fallback_enabled=(
                settings.virustotal_file_report_legacy_fallback_enabled
            ),
            routing_policy=(
                None
                if settings.virustotal_file_report_enabled and settings.virustotal_proxy_url
                else VirusTotalRoutingPolicy(routes={})
            ),
            max_response_bytes=settings.virustotal_max_response_bytes,
            default_page_size=settings.virustotal_default_page_size,
            max_page_size=settings.virustotal_max_page_size,
            max_pages=settings.virustotal_max_pages,
            max_results=settings.virustotal_max_results,
        )
        seed_enrichment = VirusTotalSeedEnrichmentService(
            vt_adapter,
            VirusTotalObservationService(BlobCatalogService(
                MinioBlobStore(
                    Minio(settings.s3_endpoint, access_key=settings.s3_access_key,
                         secret_key=settings.s3_secret_key, secure=settings.s3_secure),
                    physical_bucket=settings.s3_bucket,
                ), uow_factory
            ), uow_factory),
            VirusTotalCapabilities(file_report=settings.virustotal_file_report_enabled),
        )
        production_diagnostics = DiagnosticsLog.from_env(settings.diagnostics_log_root)
        model_gateway = create_model_gateway(settings, uow_factory)
        editorial_service = EditorialGroupingService(uow_factory)
        # Exactly one bridge capabilities provider for this worker execution
        # context; it also doubles as the ConversationSessionCloser that
        # closes the exact live Temporary Chat browser session.
        bridge_provider = create_bridge_capabilities_provider(settings)
        cumulative_discovery_service = CumulativeDiscoveryService(
            uow_factory,
            planner=ChatGptMergePlanner(
                model_gateway,
                bridge_capabilities_provider=bridge_provider,
            ),
            after_activation=editorial_service.synchronize,
            diagnostics=production_diagnostics,
        )
        job_service: JobService
        job_dispatcher = DramatiqJobDispatcher()

        async def enqueue_discovery_reconciliation(
            batch: object, input_mode: object, actor_id: str
        ) -> object:
            from cti_app.domain.discovery import DiscoveryBatch
            from cti_app.domain.discovery_cumulative import DiscoveryInputMode

            if not isinstance(batch, DiscoveryBatch) or not isinstance(
                input_mode, DiscoveryInputMode
            ):
                raise TypeError("Invalid discovery reconciliation request")
            intake, _ = await cumulative_discovery_service.ingest_batch(
                batch, input_mode=input_mode, actor_id=actor_id
            )
            parent = await cumulative_discovery_service.active_snapshot(batch.edition_id)
            parameters = ReconcileDiscoveryParameters(
                intake_id=intake.id,
                edition_id=batch.edition_id,
                expected_parent_snapshot_id=parent.id if parent else None,
                actor_id=actor_id,
            )
            try:
                child = await job_service.submit(
                    kind=RECONCILE_DISCOVERY_JOB_KIND,
                    aggregate_type="edition",
                    aggregate_id=batch.edition_id,
                    idempotency_key=f"reconcile-discovery:{intake.id}",
                    correlation_id=get_correlation_id(),
                    input_parameters=parameters.model_dump(mode="json"),
                    max_attempts=3,
                    actor_id=actor_id,
                )
                await job_dispatcher.dispatch(child.id)
                return child
            except DuplicateJobError as exc:
                return await job_service.get(exc.existing_job_id)

        discovery_service = DiscoveryService(
            uow_factory,
            model_gateway,
            archive=model_gateway,
            bridge_capabilities_provider=bridge_provider,
            after_persisted_batch=enqueue_discovery_reconciliation,
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
        model_conversation_service = ModelConversationService(
            uow_factory,
            model_gateway,
            blob_store,
            retention_days=settings.model_conversation_retention_days,
            conversation_session_closer=bridge_provider,
        )
        # Production stage jobs run here, so the worker needs the production
        # registrations and a bound chain to queue the following stage.
        production_artifact_store = ProductionArtifactStore(
            BlobCatalogService(blob_store, uow_factory)
        )
        production_chain = ProductionStageChain()
        registry = create_job_registry(
            model_gateway,
            discovery_service,
            collection_service,
            brief_service,
            uow_factory,
            model_conversation_service=model_conversation_service,
            production_chain=production_chain,
            production_artifact_store=production_artifact_store,
            production_diagnostics=production_diagnostics,
            cumulative_discovery_service=cumulative_discovery_service,
            seed_enrichment=seed_enrichment,
        )
        job_service = JobService(uow_factory, registry)
        production_chain.bind(job_service, job_dispatcher)
        executor = JobExecutor(
            uow_factory,
            registry,
            retry_base_seconds=settings.job_retry_base_seconds,
            retry_max_seconds=settings.job_retry_max_seconds,
            heartbeat_interval_seconds=min(20.0, settings.job_heartbeat_timeout_seconds / 3),
            diagnostics=production_diagnostics,
        )
        job = await executor.execute(job_id)
        if job.status is JobStatus.QUEUED and job.next_retry_at is not None:
            seconds = max(0.0, (job.next_retry_at - datetime.now(UTC)).total_seconds())
            return int(seconds * 1000)
        return None
    finally:
        if vt_direct_client is not None:
            await vt_direct_client.aclose()
        if vt_proxy_client is not None:
            await vt_proxy_client.aclose()
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
            timedelta(seconds=settings.job_heartbeat_timeout_seconds),
            resume_current_attempt_kinds=DURABLE_RESUME_JOB_KINDS,
        )
        return [job.id for job in jobs if job.status is JobStatus.QUEUED]
    finally:
        await engine.dispose()

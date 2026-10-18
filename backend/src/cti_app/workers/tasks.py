import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import dramatiq
import httpx
from minio import Minio

from cti_app.application.analyst_vt_enrichment import VirusTotalSeedEnrichmentService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.confined_analysis import CONFINED_ANALYSIS_JOB_KIND
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.discovery.cumulative.chatgpt_planner import ChatGptMergePlanner
from cti_app.application.discovery.cumulative.jobs import (
    ensure_discovery_reconciliation_job,
)
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.jobs import (
    DISCOVERY_JOB_KIND,
    REPROCESS_DISCOVERY_REPORT_JOB_KIND,
)
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.edition_publication import (
    EDITION_ASSEMBLE_JOB_KIND,
    EditionAssemblyService,
)
from cti_app.application.edition_release_materialization import (
    EditionReleaseRematerializationService,
)
from cti_app.application.edition_rendering import (
    EDITION_RENDER_JOB_KIND,
    EditionRenderService,
)
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.edition_workspace import (
    EditionProductionCheckpointService,
    EditionWorkspaceMaterializer,
)
from cti_app.application.http_collection import (
    CollectionPolicy,
    SafeHttpCollector,
    SystemDnsResolver,
    parse_domain_policy,
)
from cti_app.application.investigation_jobs import (
    make_investigation_runner,
)
from cti_app.application.jobs import (
    JobExecutor,
    JobService,
    create_job_registry,
)
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import JobUnitOfWork, UnitOfWork
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_jobs import (
    ProductionStageChain,
    production_reconciliation_resume_job_kind,
    stage_job_kind,
)
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.virustotal import VirusTotalCapabilities, VirusTotalRoutingPolicy
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.config import Settings, get_settings
from cti_app.domain.jobs import INVESTIGATION_RUN_JOB_KIND, JobStatus
from cti_app.domain.production_pipeline import production_stages
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.ghidra_mcp import create_ghidra_mcp_http_adapter
from cti_app.infrastructure.http import AsyncioPinnedHttpTransport
from cti_app.infrastructure.investigation_runtime_ports import (
    build_investigation_runtime_composition_factory,
)
from cti_app.infrastructure.jobs import DramatiqJobDispatcher
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache
from cti_app.infrastructure.quarantined_capture_source import ProductionQuarantinedCaptureSource
from cti_app.infrastructure.shodan import create_shodan_http_adapter, create_shodan_http_client
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from cti_app.infrastructure.virustotal import (
    create_virustotal_direct_http_client,
    create_virustotal_http_adapter,
    create_virustotal_http_client,
)
from cti_app.integrations.model_factory import (
    create_bridge_capabilities_provider,
    create_model_gateway,
)
from cti_app.logging import configure_logging, get_correlation_id
from cti_app.workers.broker import broker as broker

# Une recherche ChatGPT durable dépasse largement la limite Dramatiq par
# défaut de 600 000 ms, qui tuait le worker en pleine attente du bridge.
EXECUTE_JOB_TIME_LIMIT_MS = int(get_settings().job_actor_time_limit_seconds * 1000)


def _configure_worker_logging() -> None:
    configure_logging(get_settings().log_level)


_configure_worker_logging()


def virustotal_capabilities_from_settings(settings: Settings) -> VirusTotalCapabilities:
    """Translate explicit Settings opt-ins into the shared VT capability set."""
    return VirusTotalCapabilities(
        file_report=settings.virustotal_file_report_enabled,
        file_relationships=settings.virustotal_object_relationships_enabled,
        intelligence_search=settings.virustotal_intelligence_search_enabled,
        file_download=settings.virustotal_file_download_enabled,
        behaviour_reports=settings.virustotal_behaviour_reports_enabled,
        retrohunt=settings.virustotal_retrohunt_proxy_enabled,
    )


# Kinds dont l'attente survit à la perte du worker. Le processus de recovery
# les déclare explicitement pour ne pas construire tous les services métier.
DURABLE_RESUME_JOB_KINDS = frozenset(
    {
        EDITION_ASSEMBLE_JOB_KIND,
        EDITION_RENDER_JOB_KIND,
        DISCOVERY_JOB_KIND,
        # Le batch reprocessé est committé avant son handoff cumulative : perdre
        # le worker entre les deux doit reprendre le MÊME attempt (donc le même
        # context.job_id, donc le même batch déterministe), pas en brûler un.
        REPROCESS_DISCOVERY_REPORT_JOB_KIND,
        INVESTIGATION_RUN_JOB_KIND,
        *(stage_job_kind(stage) for stage in production_stages()),
        production_reconciliation_resume_job_kind(),
        CONFINED_ANALYSIS_JOB_KIND,
    }
)


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
    for job_id, kind in asyncio.run(_recover_abandoned_jobs()):
        if kind == CONFINED_ANALYSIS_JOB_KIND:
            from cti_app.workers.analysis_tasks import execute_analysis_job

            execute_analysis_job.send(str(job_id))
        else:
            execute_job.send(str(job_id))


async def _execute_job(job_id: UUID) -> int | None:
    settings = get_settings()
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)
    vt_proxy_client: httpx.AsyncClient | None = None
    vt_direct_client: httpx.AsyncClient | None = None
    retrohunt_http_client: httpx.AsyncClient | None = None
    shodan_client: httpx.AsyncClient | None = None
    ghidra_adapter = None
    vt_capabilities = virustotal_capabilities_from_settings(settings)
    if (
        settings.virustotal_retrohunt_proxy_enabled
        and settings.virustotal_retrohunt_proxy_base_url
        and settings.virustotal_retrohunt_proxy_allowed_hosts.strip()
    ):
        retrohunt_http_client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
        )

    def uow_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    try:
        if settings.virustotal_proxy_url:
            vt_proxy_client = create_virustotal_http_client(settings)
        else:
            vt_proxy_client = httpx.AsyncClient()
        if settings.virustotal_api_key and settings.virustotal_file_report_legacy_fallback_enabled:
            vt_direct_client = create_virustotal_direct_http_client(settings)
        vt_adapter = create_virustotal_http_adapter(
            client=vt_proxy_client,
            settings=settings,
            capabilities=vt_capabilities,
            direct_client=vt_direct_client,
            routing_policy=(
                None if settings.virustotal_proxy_url else VirusTotalRoutingPolicy(routes={})
            ),
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
        blob_catalog = BlobCatalogService(blob_store, uow_factory)
        production_artifact_store = ProductionArtifactStore(blob_catalog)
        vt_observations = (
            VirusTotalObservationService(blob_catalog, uow_factory)
            if vt_adapter is not None
            else None
        )
        shodan_enabled = settings.shodan_api_key is not None and any(
            (
                settings.shodan_host_enabled,
                settings.shodan_search_enabled,
                settings.shodan_domain_enabled,
                settings.shodan_count_enabled,
            )
        )
        shodan_adapter = None
        if shodan_enabled:
            shodan_client = create_shodan_http_client(settings)
            shodan_adapter = create_shodan_http_adapter(client=shodan_client, settings=settings)
        if settings.ghidra_enabled:
            ghidra_adapter = create_ghidra_mcp_http_adapter(settings)
            await ghidra_adapter.probe()
        seed_enrichment = VirusTotalSeedEnrichmentService(
            vt_adapter,
            vt_observations,
            vt_capabilities,
        )
        production_diagnostics = DiagnosticsLog.from_env(settings.diagnostics_log_root)
        model_gateway = create_model_gateway(settings, uow_factory)
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
            return await ensure_discovery_reconciliation_job(
                batch,
                input_mode=input_mode,
                actor_id=actor_id,
                cumulative_discovery_service=cumulative_discovery_service,
                job_service=job_service,
                job_dispatcher=job_dispatcher,
                correlation_id=get_correlation_id(),
            )

        discovery_service = DiscoveryService(
            uow_factory,
            model_gateway,
            archive=model_gateway,
            bridge_capabilities_provider=bridge_provider,
            after_persisted_batch=enqueue_discovery_reconciliation,
            background_poll_interval_seconds=settings.discovery_bridge_poll_interval_seconds,
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
        # Production stage jobs run here, so the worker needs the production
        # registrations and a bound chain to queue the following stage.
        model_conversation_service = ModelConversationService(
            uow_factory,
            model_gateway,
            blob_store,
            retention_days=settings.model_conversation_retention_days,
            conversation_session_closer=bridge_provider,
        )
        investigation_runtime_composition = build_investigation_runtime_composition_factory(
            settings,
            uow_factory=uow_factory,
            blob_store=blob_store,
            blob_catalog=blob_catalog,
            artifact_store=production_artifact_store,
            model_conversations=model_conversation_service,
            vt_adapter=vt_adapter,
            vt_capabilities=vt_capabilities,
            vt_observations=vt_observations,
            shodan_adapter=shodan_adapter,
            ghidra_adapter=ghidra_adapter,
            network_quarantine_factory=lambda investigation_id: ProductionQuarantinedCaptureSource(
                uow_factory=uow_factory,
                blob_catalog=blob_catalog,
                settings=settings,
                investigation_id=investigation_id,
            ),
            retrohunt_http_client=retrohunt_http_client,
        )
        investigation_composition_factory = investigation_runtime_composition.composition_factory
        investigation_runner = make_investigation_runner(
            uow_factory,
            investigation_composition_factory,
        )
        chp_typst_root, font_bundle_root, typst_fonts_lock_path = typst_bundle_paths()
        typst_compiler = TypstSubprocessCompiler()
        media_asset_store = MediaAssetStore(production_artifact_store, uow_factory)
        pdf_inspection_cache = PdfInspectionCache(settings.pdf_inspection_cache_root)
        edition_render_service = EditionRenderService(
            uow_factory=uow_factory,
            artifact_store=production_artifact_store,
            media_asset_store=media_asset_store,
            renderer=EditionTypstRenderer(),
            compiler=typst_compiler,
            chp_typst_root=chp_typst_root,
            font_bundle_root=font_bundle_root,
            typst_fonts_lock_path=typst_fonts_lock_path,
            mapping_cache=pdf_inspection_cache,
        )
        edition_workspace_materializer = EditionWorkspaceMaterializer(
            settings.edition_workspace_root
        )
        production_checkpoint = EditionProductionCheckpointService(
            uow_factory,
            production_artifact_store,
            settings.edition_workspace_root,
            materializer=edition_workspace_materializer,
            diagnostics=production_diagnostics,
        )
        publication_assembly = EditionAssemblyService(
            uow_factory,
            production_artifact_store,
        )
        edition_release_rematerializer = EditionReleaseRematerializationService(
            uow_factory,
            production_artifact_store,
            edition_workspace_materializer,
        )
        production_pacing = ProductionPacingPolicy.from_settings(settings)
        production_chain = ProductionStageChain(production_pacing)
        registry = create_job_registry(
            model_gateway,
            discovery_service,
            collection_service,
            uow_factory,
            production_chain=production_chain,
            production_artifact_store=production_artifact_store,
            production_diagnostics=production_diagnostics,
            cumulative_discovery_service=cumulative_discovery_service,
            seed_enrichment=seed_enrichment,
            production_checkpoint=production_checkpoint,
            publication_assembly=publication_assembly,
            edition_render_service=edition_render_service,
            job_dispatcher=job_dispatcher,
            bridge_transport=bridge_provider,
            production_diagram_compiler=D2DiagramCompiler(),
            production_chart_compiler=AnalyticChartRenderer(),
            edition_release_rematerializer=edition_release_rematerializer,
            investigation_runner=investigation_runner,
        )
        job_service = JobService(uow_factory, registry)
        production_chain.bind(job_service, job_dispatcher)
        executor = JobExecutor(
            uow_factory,
            registry,
            retry_base_seconds=settings.job_retry_base_seconds,
            retry_max_seconds=settings.job_retry_max_seconds,
            bridge_ui_retry_base_seconds=settings.job_bridge_ui_retry_base_seconds,
            bridge_ui_retry_max_seconds=settings.job_bridge_ui_retry_max_seconds,
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
        if retrohunt_http_client is not None:
            await retrohunt_http_client.aclose()
        if shodan_client is not None:
            await shodan_client.aclose()
        if ghidra_adapter is not None:
            await ghidra_adapter.aclose()
        await engine.dispose()


async def _recover_abandoned_jobs() -> list[tuple[UUID, str]]:
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
        return [(job.id, job.kind) for job in jobs if job.status is JobStatus.QUEUED]
    finally:
        await engine.dispose()

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, Request
from minio import Minio

from cti_app.api.briefs import router as briefs_router
from cti_app.api.collection import router as collection_router
from cti_app.api.discovery import merge_runs_router
from cti_app.api.discovery import router as discovery_router
from cti_app.api.editions import router as editions_router
from cti_app.api.editorial import router as editorial_router
from cti_app.api.health import router as health_router
from cti_app.api.jobs import router as jobs_router
from cti_app.api.model_conversations import router as model_conversations_router
from cti_app.api.production import router as production_router
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.briefs import BriefService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.discovery import DiscoveryService
from cti_app.application.discovery_cumulative import (
    RECONCILE_DISCOVERY_JOB_KIND,
    ChatGptMergePlanner,
    CumulativeDiscoveryService,
    ReconcileDiscoveryParameters,
)
from cti_app.application.discovery_manual_source_edits import ManualSourceEditService
from cti_app.application.editions import EditionService
from cti_app.application.editorial import EditorialGroupingService
from cti_app.application.http_collection import (
    CollectionPolicy,
    SafeHttpCollector,
    SystemDnsResolver,
    parse_domain_policy,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import DuplicateJobError, JobService, create_job_registry
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWork
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_diagnostics import ProductionDiagnosticsLog
from cti_app.application.production_jobs import ProductionStageChain
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.subject_production import (
    EditionProductionService,
    SubjectProductionService,
)
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.config import get_settings
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.health import InfrastructureReadinessChecker
from cti_app.infrastructure.http import AsyncioPinnedHttpTransport
from cti_app.infrastructure.jobs import DramatiqJobDispatcher
from cti_app.integrations.model_factory import (
    create_bridge_capabilities_provider,
    create_model_gateway,
)
from cti_app.logging import CorrelationIdMiddleware, configure_logging, get_correlation_id

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    readiness = InfrastructureReadinessChecker(settings)
    job_engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(job_engine)

    def uow_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    model_gateway = create_model_gateway(settings, uow_factory)
    blob_store = MinioBlobStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        physical_bucket=settings.s3_bucket,
    )
    editorial_service = EditorialGroupingService(
        uow_factory,
        materializer=SubjectWorkspaceMaterializer(blob_store),
        workspace_root=settings.subject_workspace_root,
    )
    production_diagnostics = ProductionDiagnosticsLog.from_env(settings.diagnostics_log_root)
    cumulative_discovery_service = CumulativeDiscoveryService(
        uow_factory,
        planner=ChatGptMergePlanner(
            model_gateway,
            bridge_capabilities_provider=create_bridge_capabilities_provider(settings),
        ),
        after_activation=editorial_service.synchronize,
        diagnostics=production_diagnostics,
    )
    job_service: JobService
    job_dispatcher: DramatiqJobDispatcher

    async def enqueue_discovery_reconciliation(
        batch: object, input_mode: object, actor_id: str
    ) -> object:
        from cti_app.domain.discovery import DiscoveryBatch
        from cti_app.domain.discovery_cumulative import DiscoveryInputMode

        if not isinstance(batch, DiscoveryBatch) or not isinstance(input_mode, DiscoveryInputMode):
            raise TypeError("Invalid discovery reconciliation request")
        intake, _ = await cumulative_discovery_service.ingest_batch(
            batch,
            input_mode=input_mode,
            actor_id=actor_id,
        )
        parent = await cumulative_discovery_service.active_snapshot(batch.edition_id)
        parameters = ReconcileDiscoveryParameters(
            intake_id=intake.id,
            edition_id=batch.edition_id,
            expected_parent_snapshot_id=parent.id if parent else None,
            actor_id=actor_id,
        )
        try:
            job = await job_service.submit(
                kind=RECONCILE_DISCOVERY_JOB_KIND,
                aggregate_type="edition",
                aggregate_id=batch.edition_id,
                idempotency_key=f"reconcile-discovery:{intake.id}",
                correlation_id=get_correlation_id(),
                input_parameters=parameters.model_dump(mode="json"),
                max_attempts=3,
                actor_id=actor_id,
            )
            await job_dispatcher.dispatch(job.id)
            return job
        except DuplicateJobError as exc:
            return await job_service.get(exc.existing_job_id)

    async def replan_discovery_intake(parameters: ReconcileDiscoveryParameters) -> object:
        # The parent snapshot is part of the key: the first reconciliation of this
        # intake already claimed the bare one, and this is a different attempt
        # against a state that has moved on since.
        parent = parameters.expected_parent_snapshot_id
        key = f"reconcile-discovery:{parameters.intake_id}:{parent or 'root'}"
        try:
            job = await job_service.submit(
                kind=RECONCILE_DISCOVERY_JOB_KIND,
                aggregate_type="edition",
                aggregate_id=parameters.edition_id,
                idempotency_key=key,
                correlation_id=get_correlation_id(),
                input_parameters=parameters.model_dump(mode="json"),
                max_attempts=3,
                actor_id=parameters.actor_id,
            )
            await job_dispatcher.dispatch(job.id)
            return job
        except DuplicateJobError as exc:
            return await job_service.get(exc.existing_job_id)

    cumulative_discovery_service.set_replan_intake(replan_discovery_intake)

    discovery_service = DiscoveryService(
        uow_factory,
        model_gateway,
        archive=model_gateway,
        bridge_capabilities_provider=create_bridge_capabilities_provider(settings),
        after_persisted_batch=enqueue_discovery_reconciliation,
        background_poll_interval_seconds=settings.discovery_bridge_poll_interval_seconds,
    )
    manual_source_edit_service = ManualSourceEditService(
        uow_factory, model_gateway, cumulative_discovery_service
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
    )

    # Production services
    subject_production_service = SubjectProductionService(uow_factory)
    edition_production_service = EditionProductionService(uow_factory)
    workflow_orchestrator = ProductionWorkflowOrchestrator(
        uow_factory,
        model_service=model_conversation_service,
    )

    production_artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
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
    )
    app.state.readiness = readiness
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = production_artifact_store
    job_service = JobService(uow_factory, registry)
    job_dispatcher = DramatiqJobDispatcher()
    # The registry must exist before the service that consumes it, so the
    # production stage chain is bound once both are available.
    production_chain.bind(job_service, job_dispatcher)
    app.state.job_service = job_service
    app.state.job_dispatcher = job_dispatcher
    app.state.edition_service = EditionService(uow_factory)
    app.state.identity_provider = LocalIdentityProvider()
    app.state.model_gateway = model_gateway
    app.state.model_conversation_service = model_conversation_service
    app.state.discovery_service = discovery_service
    app.state.cumulative_discovery_service = cumulative_discovery_service
    app.state.manual_source_edit_service = manual_source_edit_service
    app.state.editorial_service = editorial_service
    app.state.collection_service = collection_service
    app.state.brief_service = brief_service
    app.state.subject_production_service = subject_production_service
    app.state.edition_production_service = edition_production_service
    app.state.workflow_orchestrator = workflow_orchestrator
    yield
    await readiness.close()
    await job_engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(title="CTI Bulletin API", version="0.1.0", lifespan=lifespan)
    settings = get_settings()
    request_failures = ProductionDiagnosticsLog.from_env(settings.diagnostics_log_root)

    def record_request_failure(request: Request, error: BaseException) -> None:
        # Keyed by correlation id rather than a domain id: at this level the
        # request is all we know, and it is what the browser can be traced back
        # through. The endpoint's own event, when there is one, carries the rest.
        request_failures.record_failure(
            event="http.request_failed",
            run_id=uuid5(NAMESPACE_URL, f"http-request:{get_correlation_id()}"),
            stage="http",
            correlation_id=get_correlation_id(),
            error=error,
            http_method=request.method,
            http_path=request.url.path,
            http_query=str(request.url.query) or None,
        )

    application.add_middleware(CorrelationIdMiddleware, on_failure=record_request_failure)
    application.include_router(health_router)
    application.include_router(editions_router)
    application.include_router(discovery_router)
    application.include_router(merge_runs_router)
    application.include_router(editorial_router)
    application.include_router(jobs_router)
    application.include_router(collection_router)
    application.include_router(briefs_router)
    application.include_router(model_conversations_router)
    application.include_router(production_router)
    return application


app = create_app()

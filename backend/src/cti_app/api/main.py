from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI
from minio import Minio

from cti_app.api.briefs import router as briefs_router
from cti_app.api.collection import router as collection_router
from cti_app.api.discovery import router as discovery_router
from cti_app.api.editions import router as editions_router
from cti_app.api.editorial import router as editorial_router
from cti_app.api.health import router as health_router
from cti_app.api.jobs import router as jobs_router
from cti_app.api.model_conversations import router as model_conversations_router
from cti_app.api.production import router as production_router
from cti_app.application.briefs import BriefService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.discovery import DiscoveryService
from cti_app.application.editions import EditionService
from cti_app.application.editorial import EditorialGroupingService
from cti_app.application.evidence import SubjectEvidenceService
from cti_app.application.http_collection import (
    CollectionPolicy,
    SafeHttpCollector,
    SystemDnsResolver,
    parse_domain_policy,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import JobService, create_job_registry
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWork
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
from cti_app.logging import CorrelationIdMiddleware, configure_logging

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
        model_gateway,
        materializer=SubjectWorkspaceMaterializer(blob_store),
        workspace_root=settings.subject_workspace_root,
    )
    discovery_service = DiscoveryService(
        uow_factory,
        model_gateway,
        model_gateway,
        bridge_capabilities_provider=create_bridge_capabilities_provider(settings),
        after_discovery=partial(editorial_service.synchronize, resolve_ambiguous=False),
        allow_chatgpt_structuring_fallback=settings.discovery_chatgpt_structuring_fallback,
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
    evidence_service = SubjectEvidenceService(uow_factory)
    workflow_orchestrator = ProductionWorkflowOrchestrator(
        uow_factory,
        model_service=model_conversation_service,
    )

    registry = create_job_registry(
        model_gateway,
        discovery_service,
        collection_service,
        brief_service,
        uow_factory,
    )
    app.state.readiness = readiness
    app.state.job_service = JobService(uow_factory, registry)
    app.state.job_dispatcher = DramatiqJobDispatcher()
    app.state.edition_service = EditionService(uow_factory)
    app.state.identity_provider = LocalIdentityProvider()
    app.state.model_gateway = model_gateway
    app.state.model_conversation_service = model_conversation_service
    app.state.discovery_service = discovery_service
    app.state.editorial_service = editorial_service
    app.state.collection_service = collection_service
    app.state.brief_service = brief_service
    app.state.subject_production_service = subject_production_service
    app.state.edition_production_service = edition_production_service
    app.state.evidence_service = evidence_service
    app.state.workflow_orchestrator = workflow_orchestrator
    yield
    await readiness.close()
    await job_engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(title="CTI Bulletin API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(health_router)
    application.include_router(editions_router)
    application.include_router(discovery_router)
    application.include_router(editorial_router)
    application.include_router(jobs_router)
    application.include_router(collection_router)
    application.include_router(briefs_router)
    application.include_router(model_conversations_router)
    application.include_router(production_router)
    return application


app = create_app()

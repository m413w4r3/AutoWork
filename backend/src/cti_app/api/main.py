from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from minio import Minio

from cti_app.api.briefs import router as briefs_router
from cti_app.api.collection import router as collection_router
from cti_app.api.discovery import router as discovery_router
from cti_app.api.editions import router as editions_router
from cti_app.api.editorial import router as editorial_router
from cti_app.api.health import router as health_router
from cti_app.api.jobs import router as jobs_router
from cti_app.application.briefs import BriefService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.discovery import DiscoveryService
from cti_app.application.editions import EditionService
from cti_app.application.editorial import EditorialGroupingService
from cti_app.application.extraction import (
    ChunkingPolicy,
    EvidenceExtractionService,
    PdfParsingPolicy,
)
from cti_app.application.http_collection import (
    CollectionPolicy,
    SafeHttpCollector,
    SystemDnsResolver,
    parse_domain_policy,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import JobService, create_job_registry
from cti_app.application.persistence import UnitOfWork
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
        after_discovery=editorial_service.synchronize,
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
        EvidenceExtractionService(
            model_gateway,
            pdf_policy=PdfParsingPolicy(
                max_document_bytes=settings.pdf_max_document_bytes,
                max_pages=settings.pdf_max_pages,
                timeout_seconds=settings.pdf_parse_timeout_seconds,
                max_text_chars=settings.pdf_max_text_chars,
                max_metadata_length=settings.pdf_max_metadata_length,
            ),
            chunking_policy=ChunkingPolicy(
                max_chars=settings.qwen_chunk_max_chars,
                overlap_chars=settings.qwen_chunk_overlap_chars,
            ),
        ),
        fetch_lease_seconds=settings.collection_fetch_lease_seconds,
    )
    brief_service = BriefService(uow_factory, blob_store, model_gateway)
    registry = create_job_registry(
        model_gateway, discovery_service, collection_service, brief_service
    )
    app.state.readiness = readiness
    app.state.job_service = JobService(uow_factory, registry)
    app.state.job_dispatcher = DramatiqJobDispatcher()
    app.state.edition_service = EditionService(uow_factory)
    app.state.identity_provider = LocalIdentityProvider()
    app.state.model_gateway = model_gateway
    app.state.discovery_service = discovery_service
    app.state.editorial_service = editorial_service
    app.state.collection_service = collection_service
    app.state.brief_service = brief_service
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
    return application


app = create_app()

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cti_app.api.editions import router as editions_router
from cti_app.api.health import router as health_router
from cti_app.api.jobs import router as jobs_router
from cti_app.application.editions import EditionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import JobService, create_job_registry
from cti_app.application.persistence import UnitOfWork
from cti_app.config import get_settings
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.health import InfrastructureReadinessChecker
from cti_app.infrastructure.jobs import DramatiqJobDispatcher
from cti_app.integrations.model_factory import create_model_gateway
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
    registry = create_job_registry(model_gateway)
    app.state.readiness = readiness
    app.state.job_service = JobService(uow_factory, registry)
    app.state.job_dispatcher = DramatiqJobDispatcher()
    app.state.edition_service = EditionService(uow_factory)
    app.state.identity_provider = LocalIdentityProvider()
    app.state.model_gateway = model_gateway
    yield
    await readiness.close()
    await job_engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(title="CTI Bulletin API", version="0.1.0", lifespan=lifespan)
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(health_router)
    application.include_router(editions_router)
    application.include_router(jobs_router)
    return application


app = create_app()

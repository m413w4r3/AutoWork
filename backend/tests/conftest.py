from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.health import router
from cti_app.logging import CorrelationIdMiddleware


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(router)
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client

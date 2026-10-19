from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.health import router
from cti_app.config import get_settings
from cti_app.logging import CorrelationIdMiddleware


@pytest.fixture(autouse=True)
def fast_bridge_pacing_for_tests(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Keep normal tests fast; config tests exercise production defaults directly."""
    if request.node.path.name == "test_config.py":
        yield
        return
    monkeypatch.setenv("OPENAI_BRIDGE_MIN_SUBMISSION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("OPENAI_BRIDGE_SUBMISSION_JITTER_FRACTION", "0")
    monkeypatch.setenv("OPENAI_BRIDGE_PRE_SUBMISSION_RETRY_BASE_SECONDS", "0")
    monkeypatch.setenv("OPENAI_BRIDGE_PRE_SUBMISSION_RETRY_MAX_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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

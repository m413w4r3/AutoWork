import importlib.util
import sys
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.health import router
from cti_app.config import get_settings
from cti_app.logging import CorrelationIdMiddleware

# Declared dependencies (pyproject.toml, uv.lock) that were missing in every historical
# "pre-existing failure" run made with the system interpreter instead of backend/.venv.
_REQUIRED_MODULES = ("pefile", "yara", "multipart", "elftools")


def pytest_configure(config: pytest.Config) -> None:
    """Fail once, with the root cause, when the suite runs outside the locked environment."""
    missing = [name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if sys.version_info[:2] != (3, 12) or missing:
        raise pytest.UsageError(
            "Unsupported test environment "
            f"(python {sys.version_info.major}.{sys.version_info.minor}, "
            f"missing modules: {', '.join(missing) or 'none'}). Run the backend tests through "
            "`make test-backend` or `cd backend && uv sync --locked --python 3.12 && "
            "uv run --no-sync pytest ...`; never with the system or Conda interpreter."
        )


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

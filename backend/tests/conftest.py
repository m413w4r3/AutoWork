from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.health import router
from cti_app.domain.discovery import CandidateTopic, ContributionStatus, DiscoveryContribution
from cti_app.logging import CorrelationIdMiddleware


def wrap_candidates_in_contributions(
    candidates: list[CandidateTopic],
    status: ContributionStatus = ContributionStatus.PENDING,
) -> list[DiscoveryContribution]:
    """Helper to convert candidates to contributions for testing."""
    now = datetime.now(UTC)
    return [
        DiscoveryContribution(
            candidate=candidate,
            status=status,
            created_at=now,
            accepted_at=now if status == ContributionStatus.ACCEPTED else None,
        )
        for candidate in candidates
    ]


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

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from cti_app.application.health import DependencyStatus


class FakeReadinessChecker:
    def __init__(self, statuses: dict[str, DependencyStatus]) -> None:
        self._statuses = statuses
        self.called = False

    async def check(self) -> dict[str, DependencyStatus]:
        self.called = True
        return self._statuses


@pytest.mark.asyncio
async def test_live_has_no_dependency(client: AsyncClient, app: FastAPI) -> None:
    checker = FakeReadinessChecker({})
    app.state.readiness = checker

    response = await client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert checker.called is False


@pytest.mark.asyncio
async def test_ready_reports_all_dependencies(client: AsyncClient, app: FastAPI) -> None:
    app.state.readiness = FakeReadinessChecker(
        {
            "postgresql": DependencyStatus(status="ok"),
            "redis": DependencyStatus(status="ok"),
            "object_storage": DependencyStatus(status="ok"),
        }
    )

    response = await client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert set(response.json()["dependencies"]) == {"postgresql", "redis", "object_storage"}


@pytest.mark.asyncio
async def test_ready_returns_503_with_safe_failure_detail(
    client: AsyncClient, app: FastAPI
) -> None:
    app.state.readiness = FakeReadinessChecker(
        {
            "postgresql": DependencyStatus(status="ok"),
            "redis": DependencyStatus(status="unavailable", detail="TimeoutError"),
            "object_storage": DependencyStatus(status="ok"),
        }
    )

    response = await client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["dependencies"]["redis"] == {
        "status": "unavailable",
        "detail": "TimeoutError",
    }


@pytest.mark.asyncio
async def test_correlation_id_is_preserved(client: AsyncClient) -> None:
    response = await client.get(
        "/api/health/live", headers={"X-Correlation-ID": "test-correlation"}
    )

    assert response.headers["X-Correlation-ID"] == "test-correlation"

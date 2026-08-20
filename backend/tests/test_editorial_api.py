from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.editorial import router
from cti_app.application.editorial import EditorialGroupingService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.domain.discovery_cumulative import DiscoveryMemberReference
from cti_app.logging import CorrelationIdMiddleware
from tests.editorial_support import InMemoryEditorialUnitOfWorkFactory
from tests.test_editorial import _candidate, _edition, _snapshot


async def test_editorial_api_group_select_and_decision_audit() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    candidates = [
        _candidate("Campagne A", "https://a.example/report"),
        _candidate("Campagne B", "https://b.example/report"),
    ]
    uow.editions[edition.id] = edition
    uow.snapshots[edition.id] = _snapshot(
        edition.id,
        [
            (uuid4(), candidate, (DiscoveryMemberReference(uuid4(), candidate.id),))
            for candidate in candidates
        ],
    )
    service = EditorialGroupingService(uow)
    await service.synchronize(edition.id)
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(router)
    application.state.editorial_service = service
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        board = await client.get(f"/api/editions/{edition.id}/editorial-groups")
        group_id = board.json()["groups"][0]["id"]
        selected = await client.post(
            f"/api/editions/{edition.id}/editorial-groups/{group_id}/select",
            json={"editorial_type": "brief"},
            headers={"X-Correlation-ID": "editorial-api-test"},
        )
        decisions = await client.get(f"/api/editions/{edition.id}/editorial-groups/decisions")

    assert board.status_code == 200
    assert board.json()["automatic_selection"] is False
    selected_group = next(item for item in selected.json()["groups"] if item["id"] == group_id)
    assert selected_group["status"] == "selected"
    assert selected_group["editorial_type"] == "brief"
    assert selected_group["subject_id"] is not None
    assert decisions.json()[0]["decision_type"] == "select"
    assert decisions.json()[0]["actor_id"] == "dev-analyst"


async def test_editorial_api_applies_versioned_decisions_in_one_request() -> None:
    uow = InMemoryEditorialUnitOfWorkFactory()
    edition = _edition()
    candidates = [
        _candidate("Campagne A", "https://a.example/report"),
        _candidate("Campagne B", "https://b.example/report"),
    ]
    uow.editions[edition.id] = edition
    uow.snapshots[edition.id] = _snapshot(
        edition.id,
        [
            (uuid4(), candidate, (DiscoveryMemberReference(uuid4(), candidate.id),))
            for candidate in candidates
        ],
    )
    service = EditorialGroupingService(uow)
    await service.synchronize(edition.id)
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(router)
    application.state.editorial_service = service
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        board = (await client.get(f"/api/editions/{edition.id}/editorial-groups")).json()
        response = await client.post(
            f"/api/editions/{edition.id}/editorial-groups/decisions",
            json={
                "decisions": [
                    {
                        "group_id": board["groups"][0]["id"],
                        "version": board["groups"][0]["version"],
                        "decision": "brief",
                    },
                    {
                        "group_id": board["groups"][1]["id"],
                        "version": board["groups"][1]["version"],
                        "decision": "ignore",
                    },
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["selected_briefs"] == 1
    assert response.json()["ignored"] == 1
    assert response.json()["undecided"] == 0

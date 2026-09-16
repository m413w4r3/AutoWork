from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.editions import router
from cti_app.application.editions import EditionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.domain.editions import EditionStatus
from tests.edition_support import InMemoryEditionUnitOfWorkFactory

EDITION_PAYLOAD = {
    "country": "Iran",
    "country_code": "IR",
    "period_start": "2026-07-01",
    "period_end": "2026-07-31",
    "tlp": "AMBER",
    "languages": ["fr", "en", "fa"],
}


def _application(factory: InMemoryEditionUnitOfWorkFactory) -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()
    return application


def _assert_minimal_view(body: dict[str, object]) -> None:
    assert set(body) == {
        "country",
        "country_code",
        "period_start",
        "period_end",
        "tlp",
        "languages",
        "id",
        "state",
        "version",
        "created_at",
        "updated_at",
    }


async def test_create_returns_open_minimal_view_and_rejects_legacy_fields() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    application = _application(factory)

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        created = await client.post("/api/editions", json=EDITION_PAYLOAD)
        legacy = await client.post(
            "/api/editions",
            json={**EDITION_PAYLOAD, "target_articles": 8},
        )

    assert created.status_code == 201
    assert created.json()["state"] == EditionStatus.OPEN.value
    assert "status" not in created.json()
    assert "progress_percent" not in created.json()
    assert "allowed_transitions" not in created.json()
    _assert_minimal_view(created.json())
    assert legacy.status_code == 422


async def test_list_filters_by_state_without_status_alias() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    application = _application(factory)
    second_payload = {**EDITION_PAYLOAD, "country": "France", "country_code": "FR"}

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        created = await client.post("/api/editions", json=EDITION_PAYLOAD)
        await client.post("/api/editions", json=second_payload)
        edition_id = created.json()["id"]
        archived = await client.post(
            f"/api/editions/{edition_id}/archive", json={"version": 1}
        )
        open_editions = await client.get("/api/editions?state=open")
        archived_editions = await client.get("/api/editions?state=archived")
        legacy_filter = await client.get("/api/editions?status=archived")

    assert archived.status_code == 200
    assert open_editions.json()["total"] == 1
    assert all(item["state"] == "open" for item in open_editions.json()["items"])
    assert archived_editions.json()["total"] == 1
    assert archived_editions.json()["items"][0]["state"] == "archived"
    assert legacy_filter.json()["total"] == 2


async def test_update_uses_version_and_only_minimal_metadata() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    application = _application(factory)

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        created = await client.post("/api/editions", json=EDITION_PAYLOAD)
        edition_id = created.json()["id"]
        updated = await client.put(
            f"/api/editions/{edition_id}",
            json={**EDITION_PAYLOAD, "country": "Iran updated", "version": 1},
        )
        stale = await client.put(
            f"/api/editions/{edition_id}",
            json={**EDITION_PAYLOAD, "version": 1},
        )

    assert updated.status_code == 200
    assert updated.json()["country"] == "Iran updated"
    assert updated.json()["state"] == "open"
    assert updated.json()["version"] == 2
    _assert_minimal_view(updated.json())
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_edition_version"


async def test_archive_is_explicit_and_archived_is_terminal() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    application = _application(factory)

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        created = await client.post("/api/editions", json=EDITION_PAYLOAD)
        edition_id = created.json()["id"]
        archived = await client.post(
            f"/api/editions/{edition_id}/archive", json={"version": 1}
        )
        archived_again = await client.post(
            f"/api/editions/{edition_id}/archive", json={"version": 2}
        )
        updated = await client.put(
            f"/api/editions/{edition_id}",
            json={**EDITION_PAYLOAD, "version": 2},
        )
        transition = await client.post(
            f"/api/editions/{edition_id}/transitions",
            json={"target_status": "open", "version": 2},
        )

    assert archived.status_code == 200
    assert archived.json()["state"] == EditionStatus.ARCHIVED.value
    assert archived.json()["version"] == 2
    _assert_minimal_view(archived.json())
    assert archived_again.status_code == 409
    assert archived_again.json()["detail"]["code"] == "invalid_edition_action"
    assert updated.status_code == 409
    assert updated.json()["detail"]["code"] == "invalid_edition_action"
    assert transition.status_code == 404


async def test_edition_delete_endpoint_is_not_public() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    application = FastAPI()
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.delete(f"/api/editions/{uuid4()}?version=1")

    assert response.status_code == 405

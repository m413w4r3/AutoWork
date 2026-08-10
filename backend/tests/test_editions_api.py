from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.editions import router
from cti_app.application.editions import EditionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.logging import CorrelationIdMiddleware
from tests.edition_support import InMemoryEditionUnitOfWorkFactory


async def test_create_read_update_transition_and_filter_scenario() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    application = FastAPI()
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()
    payload = {
        "country": "Iran",
        "country_code": "IR",
        "period_start": "2026-07-01",
        "period_end": "2026-07-31",
        "tlp": "AMBER",
        "languages": ["fr", "en", "fa"],
        "target_major_articles": 2,
        "target_briefs": 6,
        "previous_edition_id": None,
        "source_profile": "iran-default",
    }

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/editions",
            json=payload,
            headers={"X-Correlation-ID": "create-iran"},
        )
        edition_id = created.json()["id"]
        fetched = await client.get(f"/api/editions/{edition_id}")
        update_payload = {**payload, "target_briefs": 8, "version": 1}
        updated = await client.put(f"/api/editions/{edition_id}", json=update_payload)
        transitioned = await client.post(
            f"/api/editions/{edition_id}/transitions",
            json={"target_status": "discovery", "version": updated.json()["version"]},
        )
        listed = await client.get("/api/editions?country_code=IR&period=2026-07")
        audit = await client.get(f"/api/editions/{edition_id}/audit")
        stale = await client.put(f"/api/editions/{edition_id}", json=update_payload)

    assert created.status_code == 201
    assert fetched.json()["country"] == "Iran"
    assert updated.json()["target_briefs"] == 8
    assert transitioned.json()["status"] == "discovery"
    assert transitioned.json()["allowed_transitions"] == ["selection", "archived"]
    assert listed.json()["total"] == 1
    assert [event["actor_id"] for event in audit.json()] == [
        "dev-analyst",
        "dev-analyst",
        "dev-analyst",
    ]
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_edition_version"

from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.editions import router
from cti_app.application.editions import EditionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.logging import CorrelationIdMiddleware
from tests.edition_support import InMemoryEditionUnitOfWorkFactory


@pytest.mark.parametrize(
    ("source", "target"),
    (
        (EditionStatus.SELECTION, EditionStatus.PRODUCTION),
        (EditionStatus.PRODUCTION, EditionStatus.REVIEW),
        (EditionStatus.REVIEW, EditionStatus.PRODUCTION),
        (EditionStatus.REVIEW, EditionStatus.ASSEMBLING),
        (EditionStatus.ASSEMBLING, EditionStatus.REVIEW),
        (EditionStatus.ASSEMBLING, EditionStatus.PUBLISHED),
    ),
)
async def test_generic_transition_requires_workflow_use_case(
    source: EditionStatus, target: EditionStatus
) -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="default",
        status=source,
    )
    factory.state[edition.id] = edition
    initial_version = edition.version
    application = FastAPI()
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/editions/{edition.id}/transitions",
            json={"target_status": target.value, "version": edition.version},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "edition_transition_requires_use_case"
    assert factory.state[edition.id].status is source
    assert factory.state[edition.id].version == initial_version
    assert [event.action for event in factory.events] == []


async def test_generic_transition_cannot_archive_assembling_edition() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="default",
        status=EditionStatus.ASSEMBLING,
    )
    factory.state[edition.id] = edition
    application = FastAPI()
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/editions/{edition.id}/transitions",
            json={"target_status": "archived", "version": edition.version},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_edition_action"
    assert factory.state[edition.id].status is EditionStatus.ASSEMBLING
    assert factory.state[edition.id].version == 1
    assert [event.action for event in factory.events] == []


async def test_generic_transition_can_archive_published_edition() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="default",
        status=EditionStatus.PUBLISHED,
    )
    factory.state[edition.id] = edition
    application = FastAPI()
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/editions/{edition.id}/transitions",
            json={"target_status": "archived", "version": edition.version},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    assert factory.state[edition.id].status is EditionStatus.ARCHIVED


async def test_generic_transition_cannot_archive_active_production() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="default",
        status=EditionStatus.PRODUCTION,
    )
    factory.state[edition.id] = edition
    application = FastAPI()
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/editions/{edition.id}/transitions",
            json={"target_status": "archived", "version": edition.version},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_production_requires_cancellation"
    assert factory.state[edition.id].status is EditionStatus.PRODUCTION


async def test_generic_transition_allows_normal_transition() -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="default",
        status=EditionStatus.DRAFT,
    )
    factory.state[edition.id] = edition
    application = FastAPI()
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/editions/{edition.id}/transitions",
            json={"target_status": EditionStatus.DISCOVERY.value, "version": edition.version},
        )

    assert response.status_code == 200
    assert response.json()["status"] == EditionStatus.DISCOVERY.value
    assert response.json()["version"] == 2
    assert [event.action for event in factory.events] == ["edition.transitioned"]


@pytest.mark.parametrize(
    "status",
    (EditionStatus.ASSEMBLING, EditionStatus.PUBLISHED, EditionStatus.ARCHIVED),
)
async def test_metadata_update_is_rejected_after_publication_freeze(
    status: EditionStatus,
) -> None:
    factory = InMemoryEditionUnitOfWorkFactory()
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="default",
        status=status,
    )
    factory.state[edition.id] = edition
    application = FastAPI()
    application.include_router(router)
    application.state.edition_service = EditionService(factory)
    application.state.identity_provider = LocalIdentityProvider()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.put(
            f"/api/editions/{edition.id}",
            json={
                "country": "France",
                "country_code": "FR",
                "period_start": "2026-07-01",
                "period_end": "2026-07-31",
                "tlp": "GREEN",
                "languages": ["fr"],
                "target_articles": 2,
                "previous_edition_id": None,
                "source_profile": "default",
                "version": 1,
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invalid_edition_action"
    assert factory.state[edition.id].target_articles == 1


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
        "target_articles": 8,
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
        update_payload = {**payload, "target_articles": 10, "version": 1}
        updated = await client.put(f"/api/editions/{edition_id}", json=update_payload)
        transitioned = await client.post(
            f"/api/editions/{edition_id}/transitions",
            json={"target_status": "discovery", "version": updated.json()["version"]},
        )
        listed = await client.get("/api/editions?country_code=IR&period=2026-07")
        audit = await client.get(f"/api/editions/{edition_id}/audit")
        stale = await client.put(f"/api/editions/{edition_id}", json=update_payload)
        archived = await client.post(
            f"/api/editions/{edition_id}/transitions",
            json={
                "target_status": "archived",
                "version": transitioned.json()["version"],
            },
        )

    assert created.status_code == 201
    assert fetched.json()["country"] == "Iran"
    assert updated.json()["target_articles"] == 10
    assert transitioned.json()["status"] == "discovery"
    assert transitioned.json()["allowed_transitions"] == ["selection", "archived"]
    assert listed.json()["total"] == 1
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert [event["actor_id"] for event in audit.json()] == [
        "dev-analyst",
        "dev-analyst",
        "dev-analyst",
    ]
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_edition_version"
    assert listed.json()["total"] == 1


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

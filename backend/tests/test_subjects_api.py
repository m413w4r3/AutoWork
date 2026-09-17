from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.editions import router as editions_router
from cti_app.api.subjects import router as subjects_router
from cti_app.application.editions import EditionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.subjects import SubjectService
from cti_app.domain.editions import EditionStatus
from tests.subject_support import InMemorySubjectUnitOfWorkFactory

EDITION_PAYLOAD = {
    "country": "Iran",
    "country_code": "IR",
    "period_start": "2026-07-01",
    "period_end": "2026-07-31",
    "tlp": "AMBER",
    "languages": ["fr", "en", "fa"],
}


def _application(
    factory: InMemorySubjectUnitOfWorkFactory,
) -> FastAPI:
    application = FastAPI()
    application.include_router(editions_router)
    application.include_router(subjects_router)
    application.state.edition_service = EditionService(factory)
    application.state.subject_service = SubjectService(factory)
    application.state.identity_provider = LocalIdentityProvider()
    return application


async def _create_subject(
    client: AsyncClient,
    factory: InMemorySubjectUnitOfWorkFactory,
) -> tuple[UUID, UUID]:
    created_edition = await client.post("/api/editions", json=EDITION_PAYLOAD)
    assert created_edition.status_code == 201
    edition_id = UUID(created_edition.json()["id"])
    subject = await SubjectService(factory).materialize(edition_id, "APT 29")
    return edition_id, subject.id


def _assert_minimal_view(body: dict[str, object]) -> None:
    assert set(body) == {
        "id",
        "edition_id",
        "title",
        "slug",
        "tlp",
        "version",
        "created_at",
        "updated_at",
    }
    assert "external_id" not in body


async def test_list_and_get_return_minimal_subject_views() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    application = _application(factory)

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        edition_id, subject_id = await _create_subject(client, factory)
        listed = await client.get(f"/api/editions/{edition_id}/subjects")
        fetched = await client.get(f"/api/subjects/{subject_id}")

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    _assert_minimal_view(listed.json()[0])
    assert listed.json()[0]["id"] == str(subject_id)
    assert listed.json()[0]["edition_id"] == str(edition_id)
    assert listed.json()[0]["title"] == "APT 29"
    assert fetched.status_code == 200
    _assert_minimal_view(fetched.json())
    assert fetched.json()["id"] == str(subject_id)


async def test_list_missing_edition_returns_stable_404() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    application = _application(factory)

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/editions/{uuid4()}/subjects")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "edition_not_found"


async def test_get_missing_subject_returns_stable_404() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    application = _application(factory)

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/subjects/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "subject_not_found"


async def test_list_subjects_from_archived_edition_is_available() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    application = _application(factory)

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        edition_id, _ = await _create_subject(client, factory)
        archived = await client.post(f"/api/editions/{edition_id}/archive", json={"version": 1})
        response = await client.get(f"/api/editions/{edition_id}/subjects")

    assert archived.status_code == 200
    assert archived.json()["state"] == EditionStatus.ARCHIVED.value
    assert response.status_code == 200
    assert len(response.json()) == 1


async def test_subject_creation_is_not_public() -> None:
    factory = InMemorySubjectUnitOfWorkFactory()
    application = _application(factory)

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post("/api/subjects", json={"title": "APT 29"})

    assert response.status_code == 404

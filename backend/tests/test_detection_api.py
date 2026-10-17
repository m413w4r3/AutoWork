from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api.detection import router
from cti_app.application.identity import Identity
from tests.test_detection_review_service import ACTOR, SOURCE_SHA256, _Fixture


class _IdentityProvider:
    async def current(self) -> Identity:
        return Identity(actor_id=ACTOR)


def _app(fixture: _Fixture) -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    application.state.detection_review_service = fixture.service
    application.state.identity_provider = _IdentityProvider()
    return application


@pytest.mark.asyncio
async def test_detection_api_lists_exact_versions_reports_and_false_positive_status() -> None:
    fixture = _Fixture(noisy=True)
    app = _app(fixture)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/investigations/{fixture.version.investigation_id}/detections"
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["versions"]) == 1
    version = body["versions"][0]
    assert version["version_id"] == str(fixture.version.id)
    assert version["source"] == fixture.version.source
    assert version["source_sha256"] == SOURCE_SHA256
    assert version["status"] == "tested"
    assert version["test_reports"][0]["corpus_sha256"] == "3" * 64
    assert version["test_reports"][0]["known_false_positive_status"] == "present"
    assert version["test_reports"][0]["known_false_positives"] == ["2" * 64]
    report = version["test_reports"][0]
    assert report["validation_status"] == "real_partial"
    assert report["provenance_counts_by_role"] == {
        "confirmed_positive": {"real": 0, "synthetic": 1},
        "goodware": {"real": 1, "synthetic": 0},
    }
    assert report["false_positive_count"] == 1
    assert report["false_positive_sha256s"] == ["2" * 64]
    positive_case = report["roles"][0]["test_cases"][0]
    assert positive_case == {
        "sha256": "1" * 64,
        "provenance": "synthetic",
        "outcome": "match",
    }


@pytest.mark.asyncio
async def test_detection_api_marks_version_without_report_untested() -> None:
    fixture = _Fixture(with_report=False)
    app = _app(fixture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/investigations/{fixture.version.investigation_id}/detections"
        )

    assert response.status_code == 200
    assert response.json()["versions"][0]["validation_status"] == "untested"
    assert response.json()["versions"][0]["test_reports"] == []


@pytest.mark.asyncio
async def test_detection_mutations_require_idempotency_key_and_human_approval_fields() -> None:
    fixture = _Fixture()
    app = _app(fixture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing_key = await client.post(
            f"/api/detections/{fixture.version.id}/approve",
            json={
                "expected_source_sha256": SOURCE_SHA256,
                "reason": "Reviewed",
                "known_limitations": "Limited synthetic test corpus.",
            },
        )
        approved = await client.post(
            f"/api/detections/{fixture.version.id}/approve",
            json={
                "expected_source_sha256": SOURCE_SHA256,
                "reason": "Exact source and test report reviewed",
                "known_limitations": "Limited synthetic test corpus.",
            },
            headers={"Idempotency-Key": "approve-http-1"},
        )
        replay = await client.post(
            f"/api/detections/{fixture.version.id}/approve",
            json={
                "expected_source_sha256": SOURCE_SHA256,
                "reason": "Exact source and test report reviewed",
                "known_limitations": "Limited synthetic test corpus.",
            },
            headers={"Idempotency-Key": "approve-http-1"},
        )

    assert missing_key.status_code == 422
    assert missing_key.json()["detail"]["code"] == "idempotency_key_required"
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["decision"]["actor"] == ACTOR
    assert approved.json()["decision"]["test_report"]["sha256"]
    assert replay.status_code == 200
    assert replay.json()["decision"]["id"] == approved.json()["decision"]["id"]


@pytest.mark.asyncio
async def test_detection_api_rejects_a_stale_source_hash() -> None:
    fixture = _Fixture()
    app = _app(fixture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/detections/{fixture.version.id}/reject",
            json={"expected_source_sha256": "f" * 64, "reason": "Wrong source"},
            headers={"Idempotency-Key": "reject-stale-source"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "detection_source_sha256_mismatch"

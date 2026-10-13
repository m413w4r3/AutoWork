from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api.analyst_opinion import router
from cti_app.application.analyst_publication_service import AnalystPublicationService
from cti_app.application.identity import Identity
from cti_app.application.publication_augmentation import PublicationAugmentationArtifactService
from tests.test_analyst_opinion_service import make_opinion_harness


class _IdentityProvider:
    async def current(self) -> Identity:
        return Identity(actor_id="editor:api")


@pytest.mark.asyncio
async def test_opinion_api_composes_revises_approves_and_publishes_without_repin() -> None:
    harness = make_opinion_harness()
    augmentation = PublicationAugmentationArtifactService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
    )
    publication_service = AnalystPublicationService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
        augmentation_service=augmentation,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.analyst_opinion_service = harness.service
    app.state.analyst_publication_service = publication_service
    app.state.identity_provider = _IdentityProvider()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        investigation_id = str(harness.investigation.id)
        compose = await client.post(
            f"/api/investigations/{investigation_id}/opinion/compose",
            json={"selection": {}},
            headers={"Idempotency-Key": "compose-1"},
        )
        assert compose.status_code == 200
        draft = compose.json()["draft"]
        assert draft["status"] == "draft"
        assert draft["provenance"]["base_publication_artifact_id"] == str(harness.base_artifact.id)

        revise = await client.post(
            f"/api/opinions/{draft['id']}/revise",
            json={
                "expected_version": draft["version"],
                "blocks": [
                    {
                        "block_type": block["type"],
                        "text": block["text"],
                        "finding_refs": block["finding_refs"],
                        "evidence_refs": block["evidence_refs"],
                        "qualification": block["qualification"],
                        "certainty": block["certainty"],
                    }
                    for block in draft["blocks"]
                ],
                "unresolved_points": draft["unresolved_points"],
            },
            headers={"Idempotency-Key": "revise-1"},
        )
        assert revise.status_code == 200
        revised = revise.json()["draft"]
        assert revised["version"] == draft["version"] + 1

        approve = await client.post(
            f"/api/opinions/{draft['id']}/approve",
            json={
                "version": revised["version"],
                "expected_sha256": revised["content_sha256"],
                "reason": "Citations and reservations reviewed",
            },
            headers={"Idempotency-Key": "approve-1"},
        )
        assert approve.status_code == 200
        approved = approve.json()
        assert approved["reviewed_version"] == revised["version"]
        assert approved["approved_version"] == revised["version"] + 1
        assert approved["approved"]["status"] == "approved"

        publish = await client.post(
            f"/api/opinions/{draft['id']}/publish",
            json={
                "version": approved["approved_version"],
                "expected_base_publication_sha256": harness.base_artifact.input_hash,
            },
            headers={"Idempotency-Key": "publish-1"},
        )
        assert publish.status_code == 200
        published = publish.json()
        assert published["publication"]["previous_publication_artifact_id"] == str(
            harness.base_artifact.id
        )
        assert published["composition_repin_required"] is True
        assert "réépinglage" in published["message"]

        current = await client.get(f"/api/investigations/{investigation_id}/opinion")
        assert current.status_code == 200
        assert len(current.json()["history"]) == 3
        assert current.json()["renderable_blocks"]
        assert current.json()["citation_violations"] == []


@pytest.mark.asyncio
async def test_opinion_mutation_routes_require_idempotency_key() -> None:
    harness = make_opinion_harness()
    app = FastAPI()
    app.include_router(router)
    app.state.analyst_opinion_service = harness.service
    app.state.identity_provider = _IdentityProvider()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/investigations/{harness.investigation.id}/opinion/compose",
            json={"selection": {}},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "idempotency_key_required"

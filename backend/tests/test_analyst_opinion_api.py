from __future__ import annotations

import hashlib
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api.analyst_opinion import router
from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.analyst_opinion_service import AnalystOpinionService
from cti_app.application.analyst_publication_service import AnalystPublicationService
from cti_app.application.identity import Identity
from cti_app.application.publication_augmentation import PublicationAugmentationArtifactService
from cti_app.application.typst_compilation import CompiledTypstDocument
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from tests.test_analyst_opinion_service import make_opinion_harness


class _IdentityProvider:
    async def current(self) -> Identity:
        return Identity(actor_id="editor:api")


class _PreviewRenderer:
    def __init__(self) -> None:
        self.rendered_synthesis: ProductionSynthesisV1 | None = None

    async def render_draft_preview(
        self,
        document,
        opinion,
        synthesis,
        *,
        infrastructure_graph=None,
        findings=(),
        evidence_observations=(),
    ) -> CompiledTypstDocument:
        del document, opinion, infrastructure_graph, findings, evidence_observations
        self.rendered_synthesis = synthesis
        content = b"%PDF-1.4\npreview fixture\n"
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            compiler="typst",
            compiler_version="0.15.1",
        )


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
    preview_renderer = _PreviewRenderer()
    app.state.analyst_opinion_preview_service = AnalystOpinionPreviewService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
        renderer=preview_renderer,
    )
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
                "expected_sha256": draft["content_sha256"],
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
                "observations": [
                    {
                        "id": "00000000-0000-0000-0000-000000000991",
                        "anchor": "lead:0001",
                        "text": (
                            "La corrélation des indices ajoute un lien absent du texte initial."
                        ),
                        "state": "inferred",
                        "relation": "qualifies",
                        "finding_refs": [str(harness.finding.id)],
                        "evidence_refs": [str(harness.observation.id)],
                    }
                ],
            },
            headers={"Idempotency-Key": "revise-1"},
        )
        assert revise.status_code == 200
        revised = revise.json()["draft"]
        assert revised["version"] == draft["version"] + 1
        assert revised["observations"][0]["anchor"] == "lead:0001"

        revise_again = await client.post(
            f"/api/opinions/{draft['id']}/revise",
            json={
                "expected_version": revised["version"],
                "expected_sha256": revised["content_sha256"],
                "blocks": [
                    {
                        "block_type": block["type"],
                        "text": block["text"],
                        "finding_refs": block["finding_refs"],
                        "evidence_refs": block["evidence_refs"],
                        "qualification": block["qualification"],
                        "certainty": block["certainty"],
                    }
                    for block in revised["blocks"]
                ],
                "unresolved_points": revised["unresolved_points"],
                "observations": [
                    {
                        **revised["observations"][0],
                        "text": "La seconde retouche conserve la réserve sur l'attribution.",
                    }
                ],
            },
            headers={"Idempotency-Key": "revise-2"},
        )
        assert revise_again.status_code == 200
        revised = revise_again.json()["draft"]
        assert revised["version"] == draft["version"] + 2

        stale_revise = await client.post(
            f"/api/opinions/{draft['id']}/revise",
            json={
                "expected_version": revised["version"] - 1,
                "expected_sha256": revised["content_sha256"],
                "blocks": [
                    {
                        "block_type": block["type"],
                        "text": block["text"],
                        "finding_refs": block["finding_refs"],
                        "evidence_refs": block["evidence_refs"],
                        "qualification": block["qualification"],
                        "certainty": block["certainty"],
                    }
                    for block in revised["blocks"]
                ],
                "unresolved_points": revised["unresolved_points"],
                "observations": [
                    {
                        **revised["observations"][0],
                        "text": "Cette retouche périmée ne doit pas être persistée.",
                    }
                ],
            },
            headers={"Idempotency-Key": "revise-stale"},
        )
        assert stale_revise.status_code == 409

        # A fresh service object simulates a process restart against persisted versions.
        app.state.analyst_opinion_service = AnalystOpinionService(
            uow_factory=harness.uow_factory,  # type: ignore[arg-type]
            artifact_store=harness.store,  # type: ignore[arg-type]
        )
        restarted = await client.get(f"/api/investigations/{investigation_id}/opinion")
        assert restarted.status_code == 200
        assert len(restarted.json()["history"]) == 3
        assert restarted.json()["draft"]["observations"][0]["text"] == (
            "La seconde retouche conserve la réserve sur l'attribution."
        )

        opinion_id = UUID(draft["id"])
        versions_before_preview = tuple(harness.uow.analyst_opinions.items[opinion_id])
        artifacts_before_preview = tuple(harness.artifacts.items)
        decisions_before_preview = tuple(harness.decisions.items)
        preview = await client.get(
            f"/api/opinions/{draft['id']}/preview.pdf",
            params={
                "version": revised["version"],
                "opinion_sha256": revised["content_sha256"],
                "base_publication_sha256": revised["provenance"]["base_publication_sha256"],
            },
        )
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "application/pdf"
        assert preview.headers["x-opinion-status"] == "draft-unapproved"
        assert preview.headers["x-pdf-sha256"] == hashlib.sha256(preview.content).hexdigest()
        assert isinstance(preview_renderer.rendered_synthesis, ProductionSynthesisV1)
        assert preview_renderer.rendered_synthesis.subject_id == harness.investigation.subject_id
        assert tuple(harness.uow.analyst_opinions.items[opinion_id]) == versions_before_preview
        assert tuple(harness.artifacts.items) == artifacts_before_preview
        assert tuple(harness.decisions.items) == decisions_before_preview

        stale_version_preview = await client.get(
            f"/api/opinions/{draft['id']}/preview.pdf",
            params={
                "version": revised["version"] - 1,
                "opinion_sha256": revised["content_sha256"],
                "base_publication_sha256": revised["provenance"]["base_publication_sha256"],
            },
        )
        assert stale_version_preview.status_code == 409

        stale_opinion_hash_preview = await client.get(
            f"/api/opinions/{draft['id']}/preview.pdf",
            params={
                "version": revised["version"],
                "opinion_sha256": "0" * 64,
                "base_publication_sha256": revised["provenance"]["base_publication_sha256"],
            },
        )
        assert stale_opinion_hash_preview.status_code == 409

        stale_base_hash_preview = await client.get(
            f"/api/opinions/{draft['id']}/preview.pdf",
            params={
                "version": revised["version"],
                "opinion_sha256": revised["content_sha256"],
                "base_publication_sha256": "0" * 64,
            },
        )
        assert stale_base_hash_preview.status_code == 409
        assert tuple(harness.uow.analyst_opinions.items[opinion_id]) == versions_before_preview

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
        assert len(current.json()["history"]) == 4
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

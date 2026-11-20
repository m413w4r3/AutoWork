from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.api.main import create_app
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editorial_proposal import EditorialProposalOrigin
from cti_app.domain.entities import SourceDocument
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_extraction import production_extraction_to_json
from tests.integration.editorial.test_proposal_adoption_postgres import _seed
from tests.test_publication_kind_resolution import _brief_v8_payload

pytestmark = pytest.mark.integration

_CANONICAL_BUCKET = "production-artifacts-canonical"


class _SessionIdentity:
    async def current(self) -> SimpleNamespace:
        return SimpleNamespace(actor_id="dev-analyst")


class _NoModelCalls:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def unexpected_call(*_args: Any, **_kwargs: Any) -> None:
            self.calls.append(name)
            raise AssertionError("Manual editorial edits must not call the model gateway")

        return unexpected_call


async def _ready_for_production_route(
    uow_factory: UnitOfWorkFactory,
) -> tuple[Any, FastAPI, _NoModelCalls]:
    # `_seed` intentionally does not persist these production inputs. Add the
    # rows needed by the real policy adapter and use IDs returned by the blob
    # catalog because this integration database is shared between test sessions.
    seeded = await _seed(
        uow_factory,
        proposal_count=0,
        seed_discovery_lineage=True,
    )
    extraction_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_extraction_to_json(seeded.extraction)
    )
    extraction_sha256 = hashlib.sha256(extraction_bytes).hexdigest()
    extraction_blob = BlobRecord(
        descriptor=BlobDescriptor(
            extraction_sha256,
            len(extraction_bytes),
            "application/json",
            _CANONICAL_BUCKET,
        )
    )
    source_bytes = b"synthetic manual-edit integration source"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source_blob = BlobRecord(
        descriptor=BlobDescriptor(
            source_sha256,
            len(source_bytes),
            "text/plain",
            "source-documents",
        )
    )
    publication_bytes = ProductionArtifactStore.canonical_json_bytes(_brief_v8_payload())
    publication_sha256 = hashlib.sha256(publication_bytes).hexdigest()
    publication_blob = BlobRecord(
        descriptor=BlobDescriptor(
            publication_sha256,
            len(publication_bytes),
            "application/json",
            _CANONICAL_BUCKET,
        )
    )
    seeded.blob_store.seed(extraction_blob.descriptor, extraction_bytes)
    seeded.blob_store.seed(source_blob.descriptor, source_bytes)
    seeded.blob_store.seed(publication_blob.descriptor, publication_bytes)
    async with uow_factory() as uow:
        stored_extraction_blob = await uow.blobs.add_if_absent(extraction_blob)
        stored_source_blob = await uow.blobs.add_if_absent(source_blob)
        stored_publication_blob = await uow.blobs.add_if_absent(publication_blob)
        await uow.production_artifacts.append(
            ProductionArtifact(
                production_run_id=seeded.run.id,
                subject_id=seeded.subject.id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=1,
                input_hash=seeded.production_input.input_hash,
                canonical_blob_id=stored_extraction_blob.id,
            )
        )
        await uow.production_input_snapshots.add(seeded.production_input)
        await uow.source_documents.add(
            SourceDocument(
                id=seeded.extraction.sources[0].source_document_id,
                subject_id=seeded.subject.id,
                blob_id=stored_source_blob.id,
                original_name="synthetic-report.txt",
                origin="synthetic integration fixture",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=TLP.AMBER,
                do_not_submit=False,
                external_llm_allowed=True,
            )
        )
        await uow.production_artifacts.append(
            ProductionArtifact(
                production_run_id=seeded.run.id,
                subject_id=seeded.subject.id,
                stage=ProductionArtifactStage.PUBLICATION,
                version=1,
                status=ProductionArtifactStatus.VERIFIED,
                input_hash=hashlib.sha256(f"{seeded.base_artifact.id}:none".encode()).hexdigest(),
                canonical_blob_id=stored_publication_blob.id,
                metadata={
                    "input_artifacts": {
                        "synthesis_artifact_id": str(seeded.base_artifact.id),
                        "editorial_enrichment_artifact_id": None,
                    }
                },
            )
        )
        await uow.commit()

    blob_store = cast(BlobStore, seeded.blob_store)
    app = create_app()
    app.state.uow_factory = uow_factory
    app.state.blob_store = blob_store
    app.state.production_artifact_store = ProductionArtifactStore(
        BlobCatalogService(blob_store, uow_factory)
    )
    model_gateway = _NoModelCalls()
    app.state.model_gateway = model_gateway
    app.state.publication_render_service = object()
    from cti_app.application.identity import LocalIdentityProvider

    app.state.identity_provider = LocalIdentityProvider("dev-analyst")
    return seeded, app, model_gateway


async def _snapshot(client: httpx.AsyncClient, subject_id: UUID) -> dict[str, Any]:
    response = await client.get(f"/api/subjects/{subject_id}/editor/snapshot")
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _manual_path(subject_id: UUID, occurrence_id: UUID) -> str:
    return f"/api/subjects/{subject_id}/editor/blocks/{occurrence_id}/manual-edit"


@pytest.mark.asyncio
async def test_manual_edit_real_route_persists_replays_and_refuses_unsupported_values(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            app_env="development",
            api_bind_host="127.0.0.1",
        ),
    )
    seeded, app, model_gateway = await _ready_for_production_route(uow_factory)
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot = await _snapshot(client, seeded.subject.id)
        assert snapshot["publication_kind"] == "brief"
        assert snapshot["capabilities"]["manual_edit"] is True
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        path = _manual_path(seeded.subject.id, UUID(title["block_id"]))
        no_op = await client.post(
            path,
            headers={"Idempotency-Key": f"manual-{uuid4().hex}"},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "rewrite_title",
                "text": title["text"],
            },
        )
        assert no_op.status_code == 422
        assert no_op.json()["detail"]["code"] == "manual_edit_no_change"
        idempotency_key = f"manual-{uuid4().hex}"
        response = await client.post(
            path,
            headers={"Idempotency-Key": idempotency_key},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "rewrite_title",
                "text": "[Synthetic actor] Synthetic report, revised for clarity",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["artifact_version"] == 2
        assert response.json()["impact_state"] == "pending"
        replay = await client.post(
            path,
            headers={"Idempotency-Key": idempotency_key},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "rewrite_title",
                "text": "[Synthetic actor] Synthetic report, revised for clarity",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == response.json()
        stale = await client.post(
            path,
            headers={"Idempotency-Key": f"manual-{uuid4().hex}"},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "rewrite_title",
                "text": "[Synthetic actor] Stale revision",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "manual_edit_stale"

        async with uow_factory() as uow:
            accepted_proposal = await uow.editorial_proposals.get_for_occurrence(
                seeded.subject.id,
                UUID(title["block_id"]),
                UUID(response.json()["id"]),
            )
        assert accepted_proposal is not None
        assert accepted_proposal.origin is EditorialProposalOrigin.MANUAL_PATCH
        assert accepted_proposal.actor_id == uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst")
        assert accepted_proposal.thread_id is None
        assert accepted_proposal.model_run_id is None
        assert accepted_proposal.model_turn_id is None

        refreshed = await _snapshot(client, seeded.subject.id)
        lead = next(block for block in refreshed["blocks"] if block["kind"] == "lead_paragraph")
        unsupported = await client.post(
            _manual_path(seeded.subject.id, UUID(lead["block_id"])),
            headers={"Idempotency-Key": f"manual-{uuid4().hex}"},
            json={
                "snapshot_id": refreshed["snapshot_id"],
                "operation": "rewrite_paragraph",
                "text": "The attacker used 203.0.113.17 as its C2 server.",
            },
        )

    assert unsupported.status_code == 422
    assert unsupported.json()["detail"]["code"] == "manual_edit_new_fact_unadmitted"
    assert "Cette nouvelle valeur technique" in unsupported.json()["detail"]["message"]
    assert model_gateway.calls == []


@pytest.mark.asyncio
async def test_manual_edit_concurrent_base_fence_has_one_winner(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            app_env="development",
            api_bind_host="127.0.0.1",
        ),
    )
    seeded, app, model_gateway = await _ready_for_production_route(uow_factory)
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot = await _snapshot(client, seeded.subject.id)
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        path = _manual_path(seeded.subject.id, UUID(title["block_id"]))
        base_payload = {
            "snapshot_id": snapshot["snapshot_id"],
            "operation": "rewrite_title",
        }
        first_key = f"manual-{uuid4().hex}"
        second_key = f"manual-{uuid4().hex}"
        assert first_key != second_key
        first, second = await asyncio.gather(
            client.post(
                path,
                headers={"Idempotency-Key": first_key},
                json={**base_payload, "text": "[Synthetic actor] Concurrent revision A"},
            ),
            client.post(
                path,
                headers={"Idempotency-Key": second_key},
                json={**base_payload, "text": "[Synthetic actor] Concurrent revision B"},
            ),
        )

    assert sorted((first.status_code, second.status_code)) == [200, 409]
    winner = first if first.status_code == 200 else second
    stale = first if first.status_code == 409 else second
    assert winner.json()["artifact_version"] == 2
    assert stale.json()["detail"]["code"] == "manual_edit_stale"
    assert "Actualisez" in stale.json()["detail"]["message"]
    assert model_gateway.calls == []

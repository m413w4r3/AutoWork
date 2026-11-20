from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.editorial_thread_common import decode_thread_request
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from tests.integration.editorial.test_proposal_adoption_postgres import _seed

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_mounted_application_queues_proposal_with_real_production_factory(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=0, seed_discovery_lineage=True)
    monkeypatch.setattr(editorial_workspace_api, "get_settings", lambda: Settings(_env_file=None))
    artifact_store = ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory))
    gateway = object()
    model_conversations = ModelConversationService(
        uow_factory,
        gateway,  # Queueing must not make a model or Bridge call.
        seeded.blob_store,
    )
    dispatched: list[str] = []
    user_instruction = "Clarifier le passage."
    monkeypatch.setattr(
        editorial_workspace_api,
        "_enqueue_editorial_thread_job",
        lambda job_id: dispatched.append(str(job_id)),
    )

    app = FastAPI()
    app.state.uow_factory = uow_factory
    app.state.blob_store = seeded.blob_store
    app.state.production_artifact_store = artifact_store
    app.state.model_gateway = gateway
    app.state.publication_render_service = object()
    app.state.model_conversation_service = model_conversations
    app.state.identity_provider = LocalIdentityProvider("dev-analyst")
    app.include_router(editorial_workspace_api.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot_response = await client.get(f"/api/subjects/{seeded.subject.id}/editor/snapshot")
        assert snapshot_response.status_code == 200
        current_snapshot = snapshot_response.json()
        assert current_snapshot["production_run_id"] == str(seeded.run.id)
        occurrence_id = current_snapshot["blocks"][0]["block_id"]
        response = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/threads/{occurrence_id}/entries",
            headers={"Idempotency-Key": "proposal-postgres-real-app-0001"},
            json={"kind": "propose", "text": user_instruction},
        )
        thread_response = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/threads/{occurrence_id}"
        )
        accept_response = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/threads/"
            f"{occurrence_id}/proposals/"
            "00000000-0000-4000-8000-000000000001/accept",
            headers={"Idempotency-Key": "accept-stays-disabled-0001"},
            json={"expected_state_version": 1, "patch_sha256": "a" * 64},
        )

    assert response.status_code == 202
    assert response.json()["generation"] == "queued"
    assert thread_response.status_code == 200
    assert thread_response.json()["timeline"][0]["body"] == user_instruction
    assert accept_response.status_code == 404
    assert accept_response.json()["detail"]["code"] == "accept_not_enabled"
    assert len(dispatched) == 1
    expected_actor_id = uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst")
    async with uow_factory() as uow:
        record = await uow.editorial_threads.get_current_by_occurrence(
            seeded.subject.id,
            seeded.run.id,
            UUID(occurrence_id),
        )
        assert record is not None
        event = next(item for item in record.events if item.actor_id == expected_actor_id)
        job = await uow.editorial_threads.get_job_for_event(event.id)
        assert job is not None
        assert str(job.id) == dispatched[0]
        assert [item.kind for item in record.events] == [EditorialThreadEventKind.USER_MESSAGE]
    request_body = await artifact_store.read_bytes(event.payload_blob_ref)
    objective, instruction = decode_thread_request(request_body)
    assert objective == "proposal"
    assert instruction == user_instruction

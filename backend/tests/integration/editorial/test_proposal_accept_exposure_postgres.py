from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.editorial_proposal import EditorialProposalState
from tests.integration.editorial.test_proposal_adoption_postgres import _seed, _service

pytestmark = pytest.mark.integration


class _SessionIdentity:
    async def current(self) -> SimpleNamespace:
        return SimpleNamespace(actor_id="dev-analyst")


class _NoModelCalls:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def unexpected_call(*_args: Any, **_kwargs: Any) -> None:
            self.calls.append(name)
            raise AssertionError("Editorial proposal acceptance must not call the model gateway")

        return unexpected_call


def _app_for_accept(
    *,
    uow_factory: UnitOfWorkFactory,
    blob_store: BlobStore,
    model_gateway: _NoModelCalls,
) -> FastAPI:
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    app.state.uow_factory = uow_factory
    app.state.blob_store = blob_store
    app.state.production_artifact_store = ProductionArtifactStore(
        BlobCatalogService(blob_store, uow_factory)
    )
    app.state.model_gateway = model_gateway
    app.state.publication_render_service = object()
    app.state.identity_provider = _SessionIdentity()
    return app


def _accept_path(subject_id: UUID, occurrence_id: UUID, proposal_id: UUID) -> str:
    return (
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/accept"
    )


@pytest.mark.asyncio
async def test_real_accept_route_replays_once_stales_sibling_and_maps_dev_actor(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `_seed` creates a unique edition per call and persists the actual IDs
    # returned by add_if_absent for content-addressed blobs in the shared DB.
    seeded = await _seed(uow_factory, proposal_count=2)
    service = _service(seeded, uow_factory)
    model_gateway = _NoModelCalls()
    app = _app_for_accept(
        uow_factory=uow_factory,
        blob_store=cast(BlobStore, seeded.blob_store),
        model_gateway=model_gateway,
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", lambda _request: service
    )
    proposal, sibling = seeded.proposals
    actor_id = uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst")
    idempotency_key = f"accept_{uuid4().hex}"
    path = _accept_path(proposal.subject_id, proposal.occurrence_id, proposal.id)
    headers = {"Idempotency-Key": idempotency_key}
    payload = {
        "expected_state_version": proposal.state_version,
        "patch_sha256": seeded.patch_sha256,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        accepted = await client.post(path, headers=headers, json=payload)
        replay = await client.post(path, headers=headers, json=payload)
        terminal_conflict = await client.post(
            path,
            headers={"Idempotency-Key": f"accept_{uuid4().hex}"},
            json=payload,
        )
        stale_sibling = await client.post(
            _accept_path(sibling.subject_id, sibling.occurrence_id, sibling.id),
            headers={"Idempotency-Key": f"accept_{uuid4().hex}"},
            json={
                "expected_state_version": sibling.state_version,
                "patch_sha256": seeded.patch_sha256,
            },
        )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == EditorialProposalState.ACCEPTED.value
    assert accepted.json()["artifact_version"] == 2
    assert accepted.json()["impact_state"] == "pending"
    assert replay.status_code == 200
    assert replay.json() == accepted.json()
    assert terminal_conflict.status_code == 409
    assert terminal_conflict.json()["detail"]["code"] == "proposal_state_conflict"
    assert stale_sibling.status_code == 409
    assert stale_sibling.json()["detail"]["code"] == "proposal_state_conflict"
    assert model_gateway.calls == []

    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        stale = await uow.editorial_proposals.get_for_occurrence(
            sibling.subject_id, sibling.occurrence_id, sibling.id
        )
        accepted_event = await uow.editorial_proposals.get_event_by_idempotency(
            proposal.id, idempotency_key
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)

    assert stored is not None and stored.state is EditorialProposalState.ACCEPTED
    assert stale is not None and stale.state is EditorialProposalState.STALE
    assert accepted_event is not None and accepted_event.actor_id == actor_id
    assert impact is not None and impact.impact_id == UUID(accepted.json()["impact_id"])

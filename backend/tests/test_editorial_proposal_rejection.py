from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionError
from cti_app.application.editorial_proposals import EditorialProposalService
from cti_app.config import Settings


@pytest.mark.asyncio
async def test_reject_route_is_versioned_and_accept_can_be_disabled_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id = uuid4()
    occurrence_id = uuid4()
    proposal_id = uuid4()
    actor_id = uuid4()
    received: dict[str, object] = {}

    async def reject(
        self,
        received_subject_id,
        received_occurrence_id,
        received_proposal_id,
        *,
        actor_id,
        expected_state_version,
        patch_sha256,
        idempotency_key,
        workspace_service,
    ):
        received.update(
            subject_id=received_subject_id,
            occurrence_id=received_occurrence_id,
            proposal_id=received_proposal_id,
            actor_id=actor_id,
            expected_state_version=expected_state_version,
            patch_sha256=patch_sha256,
            idempotency_key=idempotency_key,
            workspace_service=workspace_service,
        )
        return SimpleNamespace(id=proposal_id, state_version=8)

    async def current_actor(_request):
        return actor_id

    monkeypatch.setattr(EditorialProposalService, "reject", reject)
    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", current_actor)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    app.state.uow_factory = lambda: None
    app.state.production_artifact_store = object()
    path = (
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/reject"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            path,
            headers={"Idempotency-Key": f"reject_{uuid4().hex}"},
            json={"expected_state_version": 7, "patch_sha256": "a" * 64},
        )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(proposal_id),
        "state": "rejected",
        "state_version": 8,
    }
    assert received["subject_id"] == subject_id
    assert received["occurrence_id"] == occurrence_id
    assert received["proposal_id"] == proposal_id
    assert received["actor_id"] == actor_id
    assert received["expected_state_version"] == 7
    assert received["patch_sha256"] == "a" * 64
    assert isinstance(received["idempotency_key"], str)
    assert "workspace_service" in received
    accept_route = (
        "/api/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/accept"
    )
    assert any(
        route.path == accept_route and "POST" in route.methods
        for route in editorial_workspace_api.router.routes
    )

    settings = Settings(_env_file=None)
    assert settings.editorial_proposal_accept_enabled is True
    disabled_settings = Settings(_env_file=None, editorial_proposal_accept_enabled=False)
    monkeypatch.setattr(editorial_workspace_api, "get_settings", lambda: disabled_settings)

    def fail_if_accept_handler_runs(_request):
        raise AssertionError("disabled accept handler was called")

    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", fail_if_accept_handler_runs
    )
    accept_path = (
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/accept"
    )
    accept_headers = {"Idempotency-Key": f"accept_{uuid4().hex}"}
    accept_payload = {"expected_state_version": 7, "patch_sha256": "a" * 64}
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(accept_path, headers=accept_headers, json=accept_payload)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "accept_not_enabled"

    accept_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _AcceptSpy:
        async def adopt(self, *args, **kwargs):
            accept_calls.append((args, kwargs))
            raise EditorialProposalAdoptionError("proposal_not_found")

    def build_accept_spy(_request):
        return _AcceptSpy()

    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_adoption_service", build_accept_spy)
    enabled_app = FastAPI()
    enabled_app.include_router(editorial_workspace_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=enabled_app), base_url="http://editorial.test"
    ) as client:
        enabled_response = await client.post(
            accept_path, headers=accept_headers, json=accept_payload
        )

    assert enabled_response.status_code == 404
    assert enabled_response.json()["detail"]["code"] == "proposal_not_found"
    assert len(accept_calls) == 1
    assert accept_calls[0][0] == (subject_id, occurrence_id, proposal_id)
    assert accept_calls[0][1]["expected_state_version"] == 7
    assert accept_calls[0][1]["patch_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_reject_validation_error_does_not_echo_submitted_values() -> None:
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    subject_id = uuid4()
    occurrence_id = uuid4()
    proposal_id = uuid4()
    submitted_marker = "private submitted editorial text"
    path = (
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/reject"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            path,
            headers={"Idempotency-Key": f"reject_{uuid4().hex}"},
            json={
                "expected_state_version": 7,
                "patch_sha256": "a" * 64,
                "unexpected_text": submitted_marker,
            },
        )

    assert response.status_code == 422
    assert submitted_marker not in response.text
    errors = response.json()["detail"]
    assert errors
    assert all(set(error) == {"loc", "type", "msg"} for error in errors)


@pytest.mark.asyncio
async def test_stale_rejection_returns_typed_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    async def reject(*_args, **_kwargs):
        raise EditorialProposalApplicationError("proposal_stale")

    async def current_actor(_request):
        return uuid4()

    monkeypatch.setattr(EditorialProposalService, "reject", reject)
    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", current_actor)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    app.state.uow_factory = lambda: None
    app.state.production_artifact_store = object()
    subject_id = uuid4()
    occurrence_id = uuid4()
    proposal_id = uuid4()
    path = (
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/reject"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            path,
            headers={"Idempotency-Key": f"reject_{uuid4().hex}"},
            json={"expected_state_version": 7, "patch_sha256": "a" * 64},
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "proposal_stale",
            "message": "Les références de cette proposition ont changé. Rechargez l’atelier.",  # noqa: RUF001
        }
    }

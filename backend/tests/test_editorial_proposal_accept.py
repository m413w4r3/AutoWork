from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionError
from cti_app.config import Settings


@pytest.mark.asyncio
async def test_accept_route_stays_disabled_when_flag_is_explicitly_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=False),
    )
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{uuid4()}/editor/threads/{uuid4()}/proposals/{uuid4()}/accept",
            json={"bad": "sensitive submitted value"},
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "accept_not_enabled"
    assert "sensitive submitted value" not in response.text


@pytest.mark.asyncio
async def test_accept_route_defaults_enabled_but_missing_production_wiring_is_french_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    assert settings.editorial_proposal_accept_enabled is True
    monkeypatch.setattr(editorial_workspace_api, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{uuid4()}/editor/threads/{uuid4()}/proposals/{uuid4()}/accept",
            headers={"Idempotency-Key": f"accept_{uuid4().hex}"},
            json={"expected_state_version": 1, "patch_sha256": "a" * 64},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "editorial_proposal_accept_unavailable",
        "message": "L'acceptation des propositions est temporairement indisponible.",
    }


@pytest.mark.asyncio
async def test_accept_route_refuses_before_adoption_when_impact_consumer_is_unwired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: False)
    adoption_factory_calls = 0

    def forbidden_adoption_factory(_request):
        nonlocal adoption_factory_calls
        adoption_factory_calls += 1
        raise AssertionError("acceptance must fail before constructing adoption")

    monkeypatch.setattr(
        editorial_workspace_api,
        "_proposal_adoption_service",
        forbidden_adoption_factory,
    )
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{uuid4()}/editor/threads/{uuid4()}/proposals/{uuid4()}/accept",
            headers={"Idempotency-Key": f"accept_{uuid4().hex}"},
            json={"expected_state_version": 1, "patch_sha256": "a" * 64},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == (
        "L'acceptation des propositions est temporairement indisponible."
    )
    assert adoption_factory_calls == 0


@pytest.mark.asyncio
async def test_accept_route_error_envelope_and_no_value_reflection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: SimpleNamespace(editorial_proposal_accept_enabled=True),
    )
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{uuid4()}/editor/threads/{uuid4()}/proposals/{uuid4()}/accept",
            headers={"Idempotency-Key": "sensitive-key-value"},
            json={"expected_state_version": "sensitive-state-value", "patch_sha256": "secret"},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "accept_request_invalid"
    assert "sensitive" not in response.text
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_accept_route_maps_a_missed_fence_to_a_french_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )

    class _StaleService:
        async def adopt(self, *_args, **_kwargs):
            raise EditorialProposalAdoptionError("proposal_base_stale")

    async def current_actor(_request):
        return uuid4()

    monkeypatch.setattr(
        editorial_workspace_api,
        "_proposal_adoption_service",
        lambda _request: _StaleService(),
    )
    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", current_actor)
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{uuid4()}/editor/threads/{uuid4()}/proposals/{uuid4()}/accept",
            headers={"Idempotency-Key": f"accept_{uuid4().hex}"},
            json={"expected_state_version": 1, "patch_sha256": "a" * 64},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "proposal_base_stale",
        "message": (
            "Cette proposition repose sur une version qui a changé. Elle n’a pas été "  # noqa: RUF001
            "appliquée. Actualisez le document, puis comparez et formulez une nouvelle "
            "proposition."
        ),
    }

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.config import Settings


@pytest.mark.asyncio
async def test_accept_route_disabled_by_default_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(editorial_workspace_api, "get_settings", lambda: Settings(_env_file=None))
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

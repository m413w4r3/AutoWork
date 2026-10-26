from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from cti_app.api.investigation import get_investigation_operator_context_section
from cti_app.application.identity import (
    IdentityScope,
    LocalIdentityProvider,
    StaticTokenIdentityProvider,
)
from cti_app.config import Settings


async def test_authenticated_human_can_read_only_article_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.api import investigation as investigation_api

    investigation_id = uuid4()
    page = object()
    investigation = SimpleNamespace(id=investigation_id)

    async def _get_investigation(_identifier: UUID) -> SimpleNamespace:
        return investigation

    @asynccontextmanager
    async def _unit_of_work() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(analyst_investigations=SimpleNamespace(get=_get_investigation))

    class _OperatorContext:
        async def get_context_section(
            self,
            section: str,
            *,
            offset: int,
            limit: int,
            expected_version: int | None,
            expected_sha256: str | None,
            cursor: str | None = None,
        ) -> object:
            assert section in {"article_claims", "budget_remaining"}
            assert (offset, limit, expected_version, expected_sha256) == (0, 20, None, None)
            assert cursor is None
            return page

    operator_context = _OperatorContext()

    def _build(_uow: object, _identifier: UUID, *, operator_mode: bool) -> SimpleNamespace:
        assert operator_mode is True
        return SimpleNamespace(operator_tool_invocations=operator_context)

    monkeypatch.setattr(investigation_api, "_uow_factory", lambda _request: _unit_of_work)
    monkeypatch.setattr(
        investigation_api,
        "_composition_factory",
        lambda _request: SimpleNamespace(build=_build),
    )
    human_token = "h" * 32
    client_token = "c" * 32
    provider = StaticTokenIdentityProvider(human_token, operator_client_token=client_token)
    assert provider.mode == "authenticated"
    assert provider.verified is True
    human_identity = provider.identity_for_authorization(f"Bearer {human_token}")
    assert human_identity is not None
    assert human_identity.has_scope(IdentityScope.HUMAN_OPERATOR)
    assert not human_identity.has_scope(IdentityScope.OPERATOR_CLIENT)
    request = SimpleNamespace(
        state=SimpleNamespace(identity=human_identity),
        app=SimpleNamespace(state=SimpleNamespace(identity_provider=provider)),
    )

    result = await get_investigation_operator_context_section(
        investigation_id,
        "article_claims",
        request,
        offset=0,
        limit=20,
        expected_version=None,
        expected_sha256=None,
        cursor=None,
    )
    assert result is page

    local_provider = LocalIdentityProvider()
    local_request = SimpleNamespace(
        state=SimpleNamespace(identity=await local_provider.current()),
        app=SimpleNamespace(state=SimpleNamespace(identity_provider=local_provider)),
    )
    local_result = await get_investigation_operator_context_section(
        investigation_id,
        "article_claims",
        local_request,
        offset=0,
        limit=20,
        expected_version=None,
        expected_sha256=None,
        cursor=None,
    )
    assert local_result is page

    with pytest.raises(HTTPException) as error:
        await get_investigation_operator_context_section(
            investigation_id,
            "budget_remaining",
            request,
            offset=0,
            limit=20,
            expected_version=None,
            expected_sha256=None,
            cursor=None,
        )
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "operator_client_scope_required"

    unverified_request = SimpleNamespace(
        state=SimpleNamespace(identity=human_identity),
        app=SimpleNamespace(
            state=SimpleNamespace(identity_provider=SimpleNamespace(mode="custom", verified=False))
        ),
    )
    with pytest.raises(HTTPException) as unverified_error:
        await get_investigation_operator_context_section(
            investigation_id,
            "article_claims",
            unverified_request,
            offset=0,
            limit=20,
            expected_version=None,
            expected_sha256=None,
            cursor=None,
        )
    assert unverified_error.value.status_code == 403
    assert unverified_error.value.detail["code"] == "verified_human_operator_required"

    client_identity = provider.identity_for_authorization(f"Bearer {client_token}")
    assert client_identity is not None
    assert client_identity.has_scope(IdentityScope.OPERATOR_CLIENT)
    client_request = SimpleNamespace(
        state=SimpleNamespace(identity=client_identity),
        app=SimpleNamespace(state=SimpleNamespace(identity_provider=provider)),
    )
    client_result = await get_investigation_operator_context_section(
        investigation_id,
        "budget_remaining",
        client_request,
        offset=0,
        limit=20,
        expected_version=None,
        expected_sha256=None,
        cursor=None,
    )
    assert client_result is page


@pytest.mark.asyncio
async def test_local_human_with_settings_rejects_non_loopback_server() -> None:
    from cti_app.api.investigation import _require_verified_human_operator

    provider = LocalIdentityProvider()
    request = SimpleNamespace(
        state=SimpleNamespace(identity=await provider.current()),
        app=SimpleNamespace(
            state=SimpleNamespace(
                identity_provider=provider,
                settings=Settings(_env_file=None),
            )
        ),
        scope={"server": ("workflow-emulator", 80)},
    )

    with pytest.raises(HTTPException) as error:
        await _require_verified_human_operator(request)

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "verified_human_operator_required"

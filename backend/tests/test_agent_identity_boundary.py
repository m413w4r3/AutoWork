"""Identity boundary for agent sessions (ADR-0011 D7, orchestrator-owned wiring).

A host CLI is an untrusted local process. The loopback API in the *local* identity mode
treats every caller as the verified human, so agent sessions are refused there; in the
token-authenticated mode only the three per-session signed POST routes skip the bearer
check (their routes verify the HMAC), and nothing else does.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest

from cti_app.api.main import _agent_signed_route, create_app
from cti_app.config import get_settings

INVESTIGATION = str(uuid4())
SESSION = str(uuid4())
TASK = str(uuid4())
SIGNED_HEADERS = {
    "x-autowork-agent-session": SESSION,
    "x-autowork-agent-signature": "v1=" + "0" * 64,
    "x-autowork-agent-timestamp": "1763800000",
}
BROKER = f"/api/investigations/{INVESTIGATION}/agent-sessions/{SESSION}/tool-calls"
TASK_COMMAND = f"/api/investigations/{INVESTIGATION}/tasks/{TASK}/commands"
HANDOFFS = f"/api/investigations/{INVESTIGATION}/handoffs"


@pytest.mark.parametrize(
    ("method", "path", "headers", "expected"),
    [
        ("POST", BROKER, SIGNED_HEADERS, True),
        ("POST", TASK_COMMAND, SIGNED_HEADERS, True),
        ("POST", HANDOFFS, SIGNED_HEADERS, True),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION}/agent-sessions/{SESSION}/instructions",
            SIGNED_HEADERS,
            True,
        ),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION}/agent-sessions/{SESSION}/collect",
            SIGNED_HEADERS,
            True,
        ),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION}/agent-sessions/{SESSION}/reconcile",
            SIGNED_HEADERS,
            True,
        ),
        ("POST", BROKER, {}, False),
        ("POST", BROKER, {"x-autowork-agent-session": SESSION}, False),
        ("GET", BROKER, SIGNED_HEADERS, False),
        ("GET", HANDOFFS, SIGNED_HEADERS, False),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION}/samples/{TASK}/qualify",
            SIGNED_HEADERS,
            False,
        ),
        ("POST", f"/api/investigations/{INVESTIGATION}/envelope/approve", SIGNED_HEADERS, False),
        ("POST", f"/api/investigations/{INVESTIGATION}/agent-sessions", SIGNED_HEADERS, False),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION}/agent-sessions/{SESSION}/interrupt",
            SIGNED_HEADERS,
            False,
        ),
        ("POST", "/api/investigations/not-a-uuid/handoffs", SIGNED_HEADERS, False),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION}/agent-sessions/x/tool-calls",
            SIGNED_HEADERS,
            False,
        ),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION}/tasks/{TASK}/commands/extra",
            SIGNED_HEADERS,
            False,
        ),
        ("POST", f"/api/investigations/{INVESTIGATION}/opinion/approve", SIGNED_HEADERS, False),
    ],
)
def test_the_signed_shim_routes_skip_the_bearer(
    method: str, path: str, headers: dict[str, str], expected: bool
) -> None:
    assert _agent_signed_route(method, path, headers) is expected


@pytest.fixture
def authenticated_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("OPERATOR_API_TOKEN", "o" * 40)
    monkeypatch.delenv("M24_AGENT_SESSION_KEY", raising=False)
    get_settings.cache_clear()
    application = create_app()
    application.state.uow_factory = lambda: None
    try:
        yield application
    finally:
        get_settings.cache_clear()


@pytest.fixture
def local_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    monkeypatch.delenv("OPERATOR_CLIENT_API_TOKEN", raising=False)
    get_settings.cache_clear()
    application = create_app()
    application.state.uow_factory = lambda: None
    try:
        yield application
    finally:
        get_settings.cache_clear()


async def _call(application: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_authenticated_mode_signed_broker_route_reaches_the_hmac_check(
    authenticated_app: Any,
) -> None:
    response = await _call(
        authenticated_app,
        "POST",
        BROKER,
        headers={**SIGNED_HEADERS, "Idempotency-Key": "k" * 16},
        json={"tool_name": "vt.file_report", "schema_version": "v1", "arguments": {}},
    )
    # Not the identity middleware's 401: the route's own HMAC verifier answered
    # (stale timestamp / forged signature => 403, absent server key => 503).
    assert response.status_code in {403, 503}
    assert response.json()["detail"]["code"].startswith("agent_session")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", BROKER),
        ("GET", f"/api/investigations/{INVESTIGATION}/tasks"),
        ("POST", f"/api/investigations/{INVESTIGATION}/samples/{TASK}/qualify"),
        ("POST", f"/api/investigations/{INVESTIGATION}/envelope/approve"),
        ("POST", f"/api/investigations/{INVESTIGATION}/agent-sessions"),
        ("POST", f"/api/investigations/{INVESTIGATION}/opinion/approve"),
    ],
)
async def test_authenticated_mode_signed_headers_never_open_human_routes(
    authenticated_app: Any, method: str, path: str
) -> None:
    response = await _call(authenticated_app, method, path, headers=SIGNED_HEADERS, json={})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "operator_token_required"


@pytest.mark.asyncio
async def test_local_identity_mode_refuses_to_open_agent_sessions(local_app: Any) -> None:
    response = await _call(
        local_app,
        "POST",
        f"/api/investigations/{INVESTIGATION}/agent-sessions",
        headers={"Idempotency-Key": "k" * 16},
        json={
            "task_id": TASK,
            "adapter_kind": "codex_cli",
            "handoff_id": str(uuid4()),
            "executable": "codex",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_sessions_require_authenticated_identity"

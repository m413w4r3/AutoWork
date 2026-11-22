"""Fail-closed workspace agent client (A07, ticket W6).

All tests are offline: a missing socket, a missing token file or an empty
setting must block every action with ``503 workspace_local_unavailable`` and
never queue work for a later agent start.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cti_app.application.workspace_agent import (
    EnsureWorkspaceV1,
    WorkspaceLocalUnavailableError,
    WorkspaceUnconfiguredError,
)
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient

SUBJECT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
INVESTIGATION_ID = "22222222-2222-4222-8222-222222222222"


def _ensure_request() -> EnsureWorkspaceV1:
    return EnsureWorkspaceV1(
        delivery_code="IR2609",
        article_slug="bluemoon-cn",
        subject_id=SUBJECT_ID,
        investigation_id=INVESTIGATION_ID,
        synthesis_sha256="a" * 64,
    )


async def test_workspace_unavailable_fails_closed() -> None:
    client = HttpWorkspaceAgentClient(socket_path="", token_file="")
    try:
        probe = await client.probe()
        assert probe.status == "unconfigured"
        assert probe.instance_id is None
        with pytest.raises(WorkspaceLocalUnavailableError) as raised:
            await client.ensure_workspace(_ensure_request())
        assert isinstance(raised.value, WorkspaceUnconfiguredError)
        assert raised.value.code == "workspace_local_unavailable"
        assert raised.value.http_status == 503
    finally:
        await client.aclose()


async def test_missing_socket_is_unavailable_and_never_queues(tmp_path: Path) -> None:
    token_file = tmp_path / "auth-token"
    token_file.write_text("synthetic-token-value", encoding="ascii")
    client = HttpWorkspaceAgentClient(
        socket_path=str(tmp_path / "absent.sock"),
        token_file=token_file,
        timeout_seconds=2.0,
    )
    try:
        probe = await client.probe()
        assert probe.status == "unavailable"
        with pytest.raises(WorkspaceLocalUnavailableError) as raised:
            await client.list_workspaces()
        assert raised.value.code == "workspace_local_unavailable"
        assert raised.value.http_status == 503
    finally:
        await client.aclose()


async def test_missing_token_file_is_unavailable(tmp_path: Path) -> None:
    client = HttpWorkspaceAgentClient(
        socket_path=str(tmp_path / "workspace-agent.sock"),
        token_file=tmp_path / "absent-token",
        timeout_seconds=2.0,
    )
    try:
        probe = await client.probe()
        assert probe.status == "unavailable"
        with pytest.raises(WorkspaceLocalUnavailableError):
            await client.get_workspace("ws-" + "0" * 32)
    finally:
        await client.aclose()

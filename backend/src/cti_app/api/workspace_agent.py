"""Workspace agent status route (design §3.5, ticket W6).

Read-only and deliberately not part of global readiness: the agent being
unavailable blocks workspace actions (503 ``workspace_local_unavailable``) but
must never fail ``/api/health/ready``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from cti_app.application.workspace_agent import WorkspaceAgentPort
from cti_app.config import Settings, get_settings
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient

router = APIRouter(prefix="/api/workspace-agent", tags=["workspace-agent"])
STATUS_PROBE_TIMEOUT_SECONDS = 2.0


class WorkspaceAgentStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "unavailable", "unconfigured"]
    instance_id: str | None = None


@router.get("/status", response_model=WorkspaceAgentStatusResponse)
async def workspace_agent_status(request: Request) -> WorkspaceAgentStatusResponse:
    configured = getattr(request.app.state, "settings", None)
    settings = configured if isinstance(configured, Settings) else get_settings()
    client: WorkspaceAgentPort = HttpWorkspaceAgentClient(
        socket_path=settings.workspace_agent_socket,
        token_file=settings.workspace_agent_token_file,
        timeout_seconds=STATUS_PROBE_TIMEOUT_SECONDS,
    )
    try:
        probe = await client.probe()
    finally:
        await client.aclose()
    return WorkspaceAgentStatusResponse(status=probe.status, instance_id=probe.instance_id)

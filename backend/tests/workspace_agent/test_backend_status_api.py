"""GET /api/workspace-agent/status (design §3.5, ticket W6).

The route is a projection of the agent probe, never a readiness dependency: a
bare application with no ``app.state.readiness`` still answers 200.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cti_app.api import workspace_agent as workspace_agent_api
from cti_app.api.workspace_agent import router
from cti_app.config import Settings
from tests.workspace_agent.conftest import RunningAgent


def _settings(*, socket_path: str, token_file: str) -> Settings:
    return Settings(
        _env_file=None,
        workspace_agent_socket=socket_path,
        workspace_agent_token_file=token_file,
    )


def _client(settings: Settings) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.state.settings = settings
    return TestClient(application)


def test_status_is_unconfigured_without_settings() -> None:
    with _client(_settings(socket_path="", token_file="")) as client:
        response = client.get("/api/workspace-agent/status")
    assert response.status_code == 200
    assert response.json() == {"status": "unconfigured", "instance_id": None}


def test_status_is_unavailable_when_the_agent_is_absent(tmp_path: Path) -> None:
    token_file = tmp_path / "auth-token"
    token_file.write_text("synthetic-token", encoding="ascii")
    settings = _settings(
        socket_path=str(tmp_path / "absent.sock"),
        token_file=str(token_file),
    )
    with _client(settings) as client:
        response = client.get("/api/workspace-agent/status")
    assert response.status_code == 200
    assert response.json() == {"status": "unavailable", "instance_id": None}


def test_status_is_available_for_the_real_agent(agent_server: RunningAgent) -> None:
    settings = _settings(
        socket_path=str(agent_server.socket_path),
        token_file=str(agent_server.runtime_dir / "auth-token"),
    )
    with _client(settings) as client:
        response = client.get("/api/workspace-agent/status")
    assert response.status_code == 200
    assert response.json() == {
        "status": "available",
        "instance_id": agent_server.agent.instance_id,
    }


def test_status_route_is_not_a_readiness_dependency() -> None:
    source = inspect.getsource(workspace_agent_api)
    assert "evaluate_readiness" not in source
    assert "app.state.readiness" not in source
    assert "health_router" not in source
    assert {route.path for route in router.routes} == {"/api/workspace-agent/status"}

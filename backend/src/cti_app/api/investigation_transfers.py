"""Human transfer route (M24-X1, ADR-0011 D3/D5/D8).

``POST /api/investigations/{investigation_id}/tasks/{task_id}/transfer``
performs the whole auditable flow — workspace sync, task-engine reassignment,
new handoff version and optional destination session — in the application
service.  This module only authenticates the verified human, builds the
canonical services from ``app.state`` and maps typed refusals.

Bridge is not a destination: ``to_actor_kind`` is ``human`` or ``operator``
and an operator names ``codex_cli``/``claude_code``.  A Bridge request is an
``advisor.consult`` task, never a transfer.  The orchestrator owns
``api/main.py`` and includes the router; no route here is operator-signed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.agent_session_disclosure import (
    SessionDisclosureUnitOfWork,
    build_session_disclosure_gate,
)
from cti_app.application.agent_session_verification import (
    HmacSessionSecretDeriver,
    SessionSecretDeriver,
)
from cti_app.application.agentic_sessions import (
    AgentSessionSecretDeliveryPort,
    HostSessionReleaseService,
    SessionUnitOfWork,
)
from cti_app.application.investigation_transfers import (
    InvestigationTransferService,
    TransferError,
    TransferTaskRequest,
    TransferUnitOfWork,
)
from cti_app.application.workspace_agent import WorkspaceAgentError, WorkspaceAgentPort
from cti_app.config import Settings, get_settings
from cti_app.domain.agentic_operator import AgenticOperatorPort
from cti_app.domain.investigation_task import TaskActor, TaskActorKind
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient

router = APIRouter(prefix="/api/investigations", tags=["investigation-transfers"])


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _workspace_agent(request: Request) -> tuple[WorkspaceAgentPort, bool]:
    injected = getattr(request.app.state, "workspace_agent_port", None)
    if injected is not None:
        return cast(WorkspaceAgentPort, injected), False
    settings = _settings(request)
    return (
        HttpWorkspaceAgentClient(
            socket_path=settings.workspace_agent_socket or None,
            token_file=settings.workspace_agent_token_file or None,
        ),
        True,
    )


def _deriver(settings: Settings) -> SessionSecretDeriver | None:
    key = settings.m24_agent_session_key
    return HmacSessionSecretDeriver(key) if key is not None else None


def _operator_port(request: Request) -> AgenticOperatorPort | None:
    return cast(
        AgenticOperatorPort | None,
        getattr(request.app.state, "agentic_operator_port", None),
    )


def _secret_delivery(request: Request) -> AgentSessionSecretDeliveryPort | None:
    return cast(
        AgentSessionSecretDeliveryPort | None,
        getattr(request.app.state, "agent_session_secret_delivery", None),
    )


def _service(request: Request, workspace_agent: WorkspaceAgentPort) -> InvestigationTransferService:
    factory = cast(Callable[[], TransferUnitOfWork], _uow_factory(request))
    settings = _settings(request)
    composition = _composition_factory(request)
    disclosure = build_session_disclosure_gate(
        uow_factory=cast(Callable[[], SessionDisclosureUnitOfWork], factory),
        article_snapshots=composition.article_snapshots,
        settings=settings,
        effective_policy_for=composition.osint_open_policy_service.effective_for_investigation,
    )
    release_service = HostSessionReleaseService(
        uow_factory=cast(Callable[[], SessionUnitOfWork], factory),
        host=getattr(request.app.state, "host_sessions", None),
        wait_seconds=settings.m24_host_release_wait_seconds,
    )
    return InvestigationTransferService(
        uow_factory=factory,
        workspace_agent=workspace_agent,
        operator=_operator_port(request),
        secret_delivery=_secret_delivery(request),
        deriver=_deriver(settings),
        disclosure=disclosure,
        release_host=release_service.flush,
        cli_network_mode=settings.agent_sessions_cli_network_mode,
        task_limits=settings.task_limits(),
    )


def _transfer_http_error(exc: TransferError) -> HTTPException:
    return _http_error(exc.status_code, exc.code, str(exc), details=exc.details or None)


@router.post("/{investigation_id}/tasks/{task_id}/transfer")
async def transfer_task(
    investigation_id: UUID,
    task_id: UUID,
    body: TransferTaskRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> dict[str, Any]:
    identity = await _require_verified_human_operator(request)
    actor = TaskActor(kind=TaskActorKind.HUMAN, actor_id=identity.actor_id)
    workspace_agent, owned = _workspace_agent(request)
    try:
        result = await _service(request, workspace_agent).transfer(
            investigation_id,
            task_id=task_id,
            actor=actor,
            idempotency_key=idempotency_key,
            request=body,
        )
    except TransferError as exc:
        raise _transfer_http_error(exc) from exc
    except WorkspaceAgentError as exc:
        # Fail closed: an unreachable host workspace blocks the transfer before
        # any task or handoff write, with the UI's workspace code.
        raise _http_error(
            getattr(exc, "http_status", 503),
            exc.code,
            str(exc),
            details=getattr(exc, "details", None),
        ) from exc
    finally:
        if owned:
            await workspace_agent.aclose()
    return result.to_payload()


__all__ = ["router"]

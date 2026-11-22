"""Investigation workspace routes for the cockpit (M24-WS1, ADR-0011 D8).

The orchestrator owns ``api/main.py``; this module only exports ``router`` and
never registers itself. Every route reuses the canonical verified-human gate:
an ``OPERATOR_CLIENT`` token is refused because an agent writes through the
host supervisor or the signed tool shim, never through the cockpit routes.

Read routes degrade from the canonical ingest tables when the host agent is
absent (``workspace_available:false``); content reads and every write return
the typed ``503 workspace_local_unavailable`` instead, and the UI shows
``Workspace local indisponible``. No write is ever queued offline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.investigation import (
    _composition_factory,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.investigation_bootstrap import (
    BootstrapVtCapabilities,
    InvestigationBootstrapService,
)
from cti_app.application.investigation_workspace import (
    ConflictChoice,
    InvestigationWorkspaceService,
    WorkspaceConflictListV1,
    WorkspaceConflictResolutionV1,
    WorkspaceContentViewV1,
    WorkspaceEnsureResultV1,
    WorkspaceExportReportV1,
    WorkspaceFileListV1,
    WorkspaceNoteWriteResultV1,
    WorkspaceServiceError,
    WorkspaceStatusV1,
    WorkspaceSyncReportV1,
    WorkspaceUnitOfWorkFactory,
)
from cti_app.application.workspace_agent import (
    IDEMPOTENCY_KEY_PATTERN,
    WorkspaceAgentError,
    WorkspaceAgentPort,
)
from cti_app.config import Settings, get_settings
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient

router = APIRouter(prefix="/api/investigations", tags=["investigation-workspace"])


class NoteWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    expected_sha256: str = Field(pattern=r"^(?:[0-9a-f]{64}|new)$")
    text: str


class ConflictResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: ConflictChoice
    expected_current_sha256: str = Field(pattern=r"^(?:[0-9a-f]{64}|new)$")
    expected_proposed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    merged_text: str | None = None
    reason: str = Field(
        default="Human resolution of a host workspace conflict",
        min_length=1,
        max_length=4_000,
    )


IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    ),
]


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


def _build_service(request: Request, agent: WorkspaceAgentPort) -> InvestigationWorkspaceService:
    factory = _composition_factory(request)
    settings = _settings(request)
    uow_factory = cast(WorkspaceUnitOfWorkFactory, _uow_factory(request))
    bootstrap = InvestigationBootstrapService(
        uow_factory=cast(Any, uow_factory),
        article_snapshots=factory.article_snapshots,
        artifact_store=factory.artifact_store,
        capabilities=BootstrapVtCapabilities.from_settings(settings),
    )
    return InvestigationWorkspaceService(
        uow_factory=uow_factory,
        workspace_agent=agent,
        article_snapshots=factory.article_snapshots,
        bootstrap=bootstrap,
    )


@asynccontextmanager
async def workspace_service_scope(
    request: Request,
) -> AsyncIterator[InvestigationWorkspaceService]:
    injected = getattr(request.app.state, "investigation_workspace_service", None)
    if injected is not None:
        yield cast(InvestigationWorkspaceService, injected)
        return
    agent, owns_agent = _workspace_agent(request)
    try:
        yield _build_service(request, agent)
    finally:
        if owns_agent:
            await agent.aclose()


def _workspace_error(exc: WorkspaceServiceError) -> HTTPException:
    return _http_error(
        exc.status_code,
        exc.code,
        str(exc),
        details=exc.details or None,
    )


def _agent_error(exc: WorkspaceAgentError) -> HTTPException:
    return _http_error(exc.http_status, exc.code, exc.message, details=exc.details or None)


@router.get(
    "/{investigation_id}/workspace",
    response_model=WorkspaceStatusV1,
)
async def get_workspace_status(investigation_id: UUID, request: Request) -> WorkspaceStatusV1:
    await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.status(investigation_id)
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc


@router.post(
    "/{investigation_id}/workspace/ensure",
    response_model=WorkspaceEnsureResultV1,
)
async def ensure_workspace(investigation_id: UUID, request: Request) -> WorkspaceEnsureResultV1:
    await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.ensure_workspace(investigation_id)
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc
        except WorkspaceAgentError as exc:
            raise _agent_error(exc) from exc


@router.post(
    "/{investigation_id}/workspace/sync",
    response_model=WorkspaceSyncReportV1,
)
async def sync_workspace(investigation_id: UUID, request: Request) -> WorkspaceSyncReportV1:
    await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.sync_workspace(investigation_id)
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc
        except WorkspaceAgentError as exc:
            raise _agent_error(exc) from exc


@router.post(
    "/{investigation_id}/workspace/exports",
    response_model=WorkspaceExportReportV1,
)
async def export_workspace(investigation_id: UUID, request: Request) -> WorkspaceExportReportV1:
    await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.export_pending(investigation_id)
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc
        except WorkspaceAgentError as exc:
            raise _agent_error(exc) from exc


@router.get(
    "/{investigation_id}/workspace/files",
    response_model=WorkspaceFileListV1,
)
async def list_workspace_files(
    investigation_id: UUID,
    request: Request,
    prefix: str | None = None,
    page_token: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> WorkspaceFileListV1:
    await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.list_files(
                investigation_id, prefix=prefix, page_token=page_token, limit=limit
            )
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc
        except WorkspaceAgentError as exc:
            raise _agent_error(exc) from exc


@router.get(
    "/{investigation_id}/workspace/files/content",
    response_model=WorkspaceContentViewV1,
)
async def read_workspace_file(
    investigation_id: UUID,
    request: Request,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    expected_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
) -> WorkspaceContentViewV1:
    await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.read_file(
                investigation_id, path=path, expected_sha256=expected_sha256
            )
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc
        except WorkspaceAgentError as exc:
            raise _agent_error(exc) from exc


@router.put(
    "/{investigation_id}/workspace/files",
    response_model=WorkspaceNoteWriteResultV1,
)
async def write_workspace_note(
    investigation_id: UUID,
    payload: NoteWriteRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> WorkspaceNoteWriteResultV1:
    identity = await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.write_note(
                investigation_id,
                path=payload.path,
                expected_sha256=payload.expected_sha256,
                text=payload.text,
                actor_id=identity.actor_id,
                idempotency_key=idempotency_key,
            )
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc
        except WorkspaceAgentError as exc:
            raise _agent_error(exc) from exc


@router.get(
    "/{investigation_id}/workspace/conflicts",
    response_model=WorkspaceConflictListV1,
)
async def list_workspace_conflicts(
    investigation_id: UUID, request: Request
) -> WorkspaceConflictListV1:
    await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.list_conflicts(investigation_id)
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc
        except WorkspaceAgentError as exc:
            raise _agent_error(exc) from exc


@router.post(
    "/{investigation_id}/workspace/conflicts/{conflict_id}/resolve",
    response_model=WorkspaceConflictResolutionV1,
)
async def resolve_workspace_conflict(
    investigation_id: UUID,
    conflict_id: str,
    payload: ConflictResolveRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> WorkspaceConflictResolutionV1:
    identity = await _require_verified_human_operator(request)
    async with workspace_service_scope(request) as service:
        try:
            return await service.resolve_conflict(
                investigation_id,
                conflict_id=conflict_id,
                choice=payload.choice,
                expected_current_sha256=payload.expected_current_sha256,
                expected_proposed_sha256=payload.expected_proposed_sha256,
                merged_text=payload.merged_text,
                actor_id=identity.actor_id,
                idempotency_key=idempotency_key,
                reason=payload.reason,
            )
        except WorkspaceServiceError as exc:
            raise _workspace_error(exc) from exc
        except WorkspaceAgentError as exc:
            raise _agent_error(exc) from exc


__all__ = ["router", "workspace_service_scope"]

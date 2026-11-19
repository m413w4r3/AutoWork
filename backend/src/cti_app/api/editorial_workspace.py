from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from starlette.responses import JSONResponse

from cti_app.application.editorial_threads import (
    EditorialThreadApplicationError,
    EditorialThreadService,
)
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from cti_app.domain.editorial_workspace import EditorialWorkspaceError
from cti_app.domain.publication_kind import PublicationKind

router = APIRouter(prefix="/api", tags=["editorial-workspace"])


class EditorialWorkspaceCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    can_discuss: bool
    can_preview_revision: bool
    manual_edit: bool
    can_add_enrichment: bool
    can_preview_pdf: bool


class EditorialWorkspaceRevisionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    legacy_target_id: str | None


class EditorialWorkspaceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    kind: Literal[
        "title",
        "lead_paragraph",
        "section_heading",
        "paragraph",
        "table",
        "diagram",
        "chart",
        "figure",
    ]
    parent_block_id: UUID | None
    position: int
    text: str | None
    evidence_refs: list[dict[str, object]]
    revision_target: EditorialWorkspaceRevisionTarget
    before_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    allowed_operations: list[str]
    revision_status: Literal["ready", "proposal_pending", "stale", "needs_review"]


class EditorialDocumentOutlineNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    label: str
    children: list[UUID]


class EditorialWorkspaceSnapshotView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: UUID
    production_run_id: UUID
    publication_kind: PublicationKind | None
    synthesis_artifact_id: UUID
    synthesis_version: int
    synthesis_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    enrichment_artifact_id: UUID | None
    enrichment_version: int | None
    publication_artifact_id: UUID | None
    publication_version: int | None
    pipeline_generation: int
    blocks: list[EditorialWorkspaceBlock]
    enrichments: list[EditorialWorkspaceBlock]
    document_outline: list[EditorialDocumentOutlineNode]
    capabilities: EditorialWorkspaceCapabilities
    preview_status: Literal["IN_PROGRESS", "READY", "FAILED", "STALE"]
    snapshot_id: UUID


class ResolveWorkspaceIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    operation: str


class ResolveWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    intent: ResolveWorkspaceIntent


class ResolveWorkspaceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    block_id: UUID
    path: str
    before_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: str


class EditorialThreadBindingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: UUID
    production_run_id: UUID
    occurrence_id: UUID
    artifact_id: UUID
    artifact_version: int
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EditorialThreadTimelineEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sequence: int
    kind: EditorialThreadEventKind
    actor_id: UUID | None
    proposal_id: UUID | None
    created_at: datetime
    body: str | None


class UnopenedThreadReadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["unopened"]
    binding: EditorialThreadBindingView
    current: EditorialThreadBindingView
    bridge_status: Literal[
        "ready",
        "waiting_for_capacity",
        "generating",
        "reply_available",
        "refused",
        "session_lost",
        "reconciliation_required",
    ]
    event_sequence: int
    timeline: list[EditorialThreadTimelineEventView]
    etag: str


class OpenThreadReadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["open"]
    thread_id: UUID
    origin: EditorialThreadBindingView
    current: EditorialThreadBindingView
    binding_state: Literal["valid"]
    bridge_status: Literal[
        "ready",
        "waiting_for_capacity",
        "generating",
        "reply_available",
        "refused",
        "session_lost",
        "reconciliation_required",
    ]
    event_sequence: int
    timeline: list[EditorialThreadTimelineEventView]
    etag: str


class StaleThreadReadView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["stale"]
    thread_id: UUID | None
    origin: EditorialThreadBindingView | None
    current: EditorialThreadBindingView
    binding_state: Literal["stale", "ambiguous"]
    bridge_status: Literal[
        "ready",
        "waiting_for_capacity",
        "generating",
        "reply_available",
        "refused",
        "session_lost",
        "reconciliation_required",
    ]
    event_sequence: int
    timeline: list[EditorialThreadTimelineEventView]
    etag: str


EditorialThreadReadView = Annotated[
    UnopenedThreadReadView | OpenThreadReadView | StaleThreadReadView,
    Field(discriminator="kind"),
]
_THREAD_READ_ADAPTER: TypeAdapter[EditorialThreadReadView] = TypeAdapter(EditorialThreadReadView)


_ERROR_MESSAGES = {
    "subject_not_found": "Le sujet demandé est introuvable.",
    "production_run_missing": "Aucune production n'est disponible pour ce sujet.",
    "publication_kind_unresolved": (
        "Le type de publication est indéterminé. L'atelier reste en lecture seule."
    ),
    "editorial_snapshot_stale": "Le document a changé. Rechargez l'atelier.",
    "editorial_identity_ambiguous": (
        "L'identité de ce bloc historique est ambiguë. Il reste en lecture seule."
    ),
    "editorial_block_missing": "Ce bloc n'existe plus dans cette version.",
    "editorial_before_hash_mismatch": ("Ce bloc a changé. Rechargez l'atelier avant de réessayer."),
    "editorial_anchor_stale": (
        "Le paragraphe d'ancrage a changé ou a été supprimé. Repositionnez l'élément."
    ),
    "editorial_registry_conflict": (
        "La continuité de ce bloc ne peut pas être prouvée. Une revue est nécessaire."
    ),
    "thread_not_found": "Discussion introuvable.",
    "thread_binding_stale": (
        "Ce paragraphe a changé. Rechargez l’atelier."  # noqa: RUF001
    ),
    "occurrence_ambiguous": (
        "L’identité de ce paragraphe historique est ambiguë."  # noqa: RUF001
    ),
    "thread_body_unavailable": "Le contenu de cette discussion est temporairement indisponible.",
    "thread_body_hash_mismatch": (
        "Le contenu de cette discussion ne correspond plus à son empreinte."
    ),
}


def _service(request: Request) -> EditorialWorkspaceService:
    artifact_store = getattr(request.app.state, "production_artifact_store", None)
    if artifact_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "editorial_snapshot_storage_unavailable",
                "message": "Le stockage des documents éditoriaux est indisponible.",
            },
        )
    return EditorialWorkspaceService(
        uow_factory=request.app.state.uow_factory,
        artifact_store=artifact_store,
    )


def _thread_service(request: Request) -> EditorialThreadService:
    workspace_service = _service(request)
    return EditorialThreadService(
        uow_factory=request.app.state.uow_factory,
        workspace_service=workspace_service,
        artifact_store=request.app.state.production_artifact_store,
    )


def _raise_workspace_error(error: EditorialWorkspaceError) -> None:
    code = error.code if error.code != "editorial_workspace_error" else str(error)
    status_code = status.HTTP_409_CONFLICT
    if code in {"subject_not_found", "production_run_missing", "editorial_block_missing"}:
        status_code = status.HTTP_404_NOT_FOUND
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": _ERROR_MESSAGES.get(code, "L'atelier est indisponible.")},
    ) from error


@router.get(
    "/subjects/{subject_id}/editor/snapshot",
    response_model=EditorialWorkspaceSnapshotView,
)
async def get_editorial_workspace_snapshot(
    subject_id: UUID, request: Request
) -> EditorialWorkspaceSnapshotView:
    try:
        payload = await _service(request).snapshot(subject_id)
    except EditorialWorkspaceError as error:
        _raise_workspace_error(error)
    return EditorialWorkspaceSnapshotView.model_validate(payload)


@router.post(
    "/subjects/{subject_id}/editor/resolve",
    response_model=ResolveWorkspaceResponse,
)
async def resolve_editorial_workspace_block(
    subject_id: UUID,
    body: ResolveWorkspaceRequest,
    request: Request,
) -> ResolveWorkspaceResponse:
    try:
        payload = await _service(request).resolve(
            subject_id=subject_id,
            snapshot_id=body.snapshot_id,
            block_id=body.intent.block_id,
            operation=body.intent.operation,
        )
    except EditorialWorkspaceError as error:
        _raise_workspace_error(error)
    return ResolveWorkspaceResponse.model_validate(payload)


@router.get(
    "/subjects/{subject_id}/editor/threads/{occurrence_id}",
    response_model=EditorialThreadReadView,
)
async def get_editorial_thread(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> EditorialThreadReadView | Response:
    try:
        result = await _thread_service(request).read(
            subject_id,
            occurrence_id,
            after_sequence=after_sequence,
            etag=if_none_match,
        )
    except EditorialThreadApplicationError as error:
        code = error.code
        response_status = (
            status.HTTP_404_NOT_FOUND
            if code == "thread_not_found"
            else status.HTTP_409_CONFLICT
            if code in {"thread_binding_stale", "occurrence_ambiguous"}
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(
            response_status,
            detail={
                "code": code,
                "message": _ERROR_MESSAGES.get(code, "La discussion est indisponible."),
            },
        ) from None
    except EditorialWorkspaceError as error:
        if error.code == "subject_not_found":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "thread_not_found",
                    "message": _ERROR_MESSAGES["thread_not_found"],
                },
            ) from None
        _raise_workspace_error(error)

    headers = {"ETag": result.etag}
    if result.not_modified:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    view = _THREAD_READ_ADAPTER.validate_python(result, from_attributes=True)
    return JSONResponse(content=view.model_dump(mode="json"), headers=headers)

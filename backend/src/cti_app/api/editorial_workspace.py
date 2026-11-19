from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from starlette.responses import JSONResponse

from cti_app.application.editorial_proposals import (
    EditorialProposalApplicationError,
    EditorialProposalService,
)
from cti_app.application.editorial_thread_common import is_valid_editorial_idempotency_key
from cti_app.application.editorial_threads import (
    EditorialThreadApplicationError,
    EditorialThreadReconciliationResult,
    EditorialThreadService,
    EditorialThreadSessionStartResult,
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
    reconciliation_outcome: (
        Literal["reconciled_reply", "not_submitted", "still_unknown", "session_lost"] | None
    ) = None


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
    previous_sessions: list[int] = Field(default_factory=list)


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


class EditorialThreadSendResultView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["accepted", "already_known"]
    thread_id: UUID
    entry_id: UUID
    generation: Literal[
        "queued",
        "waiting_for_capacity",
        "running",
        "reconciliation_required",
        "complete",
        "failed",
        "cancelled",
    ]


class EditorialThreadSessionStartResultView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["session_started"]
    thread_id: UUID
    session_number: int


class EditorialThreadReconciliationResultView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    outcome: Literal["reconciled_reply", "not_submitted", "still_unknown", "session_lost"]
    message: str


class EditorialProposalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    subject_id: UUID
    production_run_id: UUID
    occurrence_id: UUID
    thread_id: UUID | None
    state: Literal[
        "preparing",
        "proposed",
        "refused",
        "needs_review",
        "accepted",
        "rejected",
        "stale",
    ]
    state_version: int
    operation: str
    base_artifact_id: UUID
    base_artifact_version: int
    base_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    patch: dict[str, object] | None
    diagnostics: dict[str, object]
    created_at: str


EditorialThreadEntryResultView = Annotated[
    EditorialThreadSendResultView | EditorialThreadSessionStartResultView,
    Field(discriminator="kind"),
]


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
    "thread_body_unavailable": "Le contenu de ce fil est temporairement indisponible.",
    "thread_body_hash_mismatch": (
        "Le contenu de cette discussion ne correspond plus à son empreinte."
    ),
    "propose_not_enabled": "La proposition n’est pas encore activée.",  # noqa: RUF001
    "editorial_thread_busy": "Une réponse est déjà en cours pour ce paragraphe.",
    "external_llm_blocked": "La politique interdit l’envoi externe de ce contenu.",  # noqa: RUF001
    "bridge_unavailable": "Le Bridge est indisponible. Actualisez avant de continuer.",
    "reconciliation_required": (
        "L’envoi a peut-être déjà été reçu. Réconciliez-le avant de continuer."  # noqa: RUF001
    ),
    "idempotency_conflict": "Cette clé est déjà associée à une autre demande.",
    "idempotency_key_required": "Une clé d’idempotence est requise pour envoyer ce message.",  # noqa: RUF001
    "idempotency_key_invalid": "La clé d’idempotence est invalide.",  # noqa: RUF001
    "editorial_message_invalid": "Saisissez un message avant de l’envoyer.",  # noqa: RUF001
    "editorial_thread_service_unavailable": "La discussion est temporairement indisponible.",
    "proposal_not_found": "Proposition introuvable.",
    "proposal_patch_unavailable": (
        "Le contenu de cette proposition est temporairement indisponible."
    ),
    "proposal_patch_hash_mismatch": ("Le contenu vérifié de cette proposition ne correspond plus."),
    "editorial_thread_conversation_missing": "La conversation de ce fil est indisponible.",
    "editorial_thread_job_missing": "La génération de ce message est indisponible.",
    "editorial_thread_session_not_lost": (
        "La session actuelle est encore disponible. Aucune nouvelle discussion n’a été créée."  # noqa: RUF001
    ),
    "session_lost": "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre.",
    "editorial_reconciliation_invalid": "La requête de réconciliation est invalide.",
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
        model_conversations=getattr(request.app.state, "model_conversation_service", None),
        enqueue_job=_enqueue_editorial_thread_job,
    )


def _enqueue_editorial_thread_job(job_id: UUID) -> None:
    from cti_app.workers.tasks import execute_editorial_thread_job

    execute_editorial_thread_job.send(str(job_id))


async def _thread_actor_id(request: Request) -> UUID:
    identity = getattr(request.state, "identity", None)
    if identity is None:
        provider = getattr(request.app.state, "identity_provider", None)
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "operator_identity_required",
                    "message": "Authentification requise.",
                },
            )
        identity = await provider.current()
    actor_id = str(getattr(identity, "actor_id", "")).strip()
    if not actor_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "operator_identity_required", "message": "Authentification requise."},
        )
    try:
        return UUID(actor_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"autowork-actor:{actor_id}")


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


@router.get(
    "/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}",
    response_model=EditorialProposalView,
)
async def get_editorial_proposal(
    subject_id: UUID,
    occurrence_id: UUID,
    proposal_id: UUID,
    request: Request,
) -> EditorialProposalView:
    service = EditorialProposalService(
        uow_factory=request.app.state.uow_factory,
        artifact_store=request.app.state.production_artifact_store,
    )
    try:
        view = await service.inspect(
            subject_id,
            occurrence_id,
            proposal_id,
            workspace_service=_service(request),
        )
    except EditorialProposalApplicationError as error:
        code = (
            error.code
            if error.code in {"proposal_patch_hash_mismatch", "proposal_patch_unavailable"}
            else "proposal_not_found"
        )
        raise HTTPException(
            status.HTTP_404_NOT_FOUND
            if code == "proposal_not_found"
            else status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": code, "message": _ERROR_MESSAGES[code]},
        ) from None
    return EditorialProposalView.model_validate(view, from_attributes=True)


@router.post(
    "/subjects/{subject_id}/editor/threads/{occurrence_id}/entries",
    response_model=EditorialThreadEntryResultView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_editorial_thread_entry(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    payload: dict[str, object] = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EditorialThreadSendResultView | EditorialThreadSessionStartResultView:
    if idempotency_key is not None and not is_valid_editorial_idempotency_key(idempotency_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "idempotency_key_invalid",
                "message": _ERROR_MESSAGES["idempotency_key_invalid"],
            },
        )
    if payload.get("kind") == "propose":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "propose_not_enabled",
                "message": _ERROR_MESSAGES["propose_not_enabled"],
            },
        )
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "idempotency_key_required",
                "message": _ERROR_MESSAGES["idempotency_key_required"],
            },
        )
    start_new_session = payload.get("start_new_session") is True
    allowed_shape = (
        set(payload) == {"kind", "start_new_session"}
        if start_new_session
        else set(payload) == {"kind", "text"}
    )
    if not allowed_shape or payload.get("kind") != "message":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "editorial_message_invalid",
                "message": _ERROR_MESSAGES["editorial_message_invalid"],
            },
        )
    text_value = payload.get("text")
    if not start_new_session and not isinstance(text_value, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "editorial_message_invalid",
                "message": _ERROR_MESSAGES["editorial_message_invalid"],
            },
        )
    try:
        actor_id = await _thread_actor_id(request)
        service = _thread_service(request)
        if start_new_session:
            session: EditorialThreadSessionStartResult = await service.start_new_session(
                subject_id,
                occurrence_id,
                idempotency_key=idempotency_key,
            )
            return EditorialThreadSessionStartResultView(
                kind="session_started",
                thread_id=session.thread_id,
                session_number=session.session_number,
            )
        if not isinstance(text_value, str):
            raise EditorialThreadApplicationError("editorial_message_invalid")
        result = await service.send_message(
            subject_id,
            occurrence_id,
            text=text_value,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
    except EditorialThreadApplicationError as error:
        code = error.code
        response_status = (
            status.HTTP_404_NOT_FOUND
            if code == "thread_not_found"
            else status.HTTP_403_FORBIDDEN
            if code == "external_llm_blocked"
            else status.HTTP_409_CONFLICT
            if code
            in {
                "editorial_thread_busy",
                "editorial_thread_session_not_lost",
                "session_lost",
                "reconciliation_required",
                "idempotency_conflict",
            }
            else status.HTTP_400_BAD_REQUEST
            if code == "idempotency_key_invalid"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
            if code == "editorial_message_invalid"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(
            response_status,
            detail={
                "code": code,
                "message": _ERROR_MESSAGES.get(code, "La discussion est indisponible."),
            },
        ) from None
    return EditorialThreadSendResultView(
        kind=result.kind,
        thread_id=result.thread_id,
        entry_id=result.entry_id,
        generation=result.generation.value,
    )


@router.post(
    "/subjects/{subject_id}/editor/threads/{occurrence_id}/reconcile",
    response_model=EditorialThreadReconciliationResultView,
)
async def reconcile_editorial_thread_entry(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EditorialThreadReconciliationResultView:
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "idempotency_key_required",
                "message": _ERROR_MESSAGES["idempotency_key_required"],
            },
        )
    if not is_valid_editorial_idempotency_key(idempotency_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "idempotency_key_invalid",
                "message": _ERROR_MESSAGES["idempotency_key_invalid"],
            },
        )
    if await request.body():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "editorial_reconciliation_invalid",
                "message": _ERROR_MESSAGES["editorial_reconciliation_invalid"],
            },
        )
    try:
        result: EditorialThreadReconciliationResult = await _thread_service(request).reconcile(
            subject_id,
            occurrence_id,
            actor_id=await _thread_actor_id(request),
            idempotency_key=idempotency_key,
        )
    except EditorialThreadApplicationError as error:
        code = error.code
        response_status = (
            status.HTTP_404_NOT_FOUND
            if code == "thread_not_found"
            else status.HTTP_409_CONFLICT
            if code in {"reconciliation_required", "idempotency_conflict"}
            else status.HTTP_400_BAD_REQUEST
            if code == "idempotency_key_invalid"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(
            response_status,
            detail={
                "code": code,
                "message": _ERROR_MESSAGES.get(code, "La réconciliation est indisponible."),
            },
        ) from None
    return EditorialThreadReconciliationResultView(
        thread_id=result.thread_id,
        outcome=result.outcome.value,
        message=result.message,
    )

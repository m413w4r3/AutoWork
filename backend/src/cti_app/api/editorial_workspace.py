from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.responses import JSONResponse

from cti_app.application.editorial_drafts import (
    MAX_DRAFT_BYTES,
    EditorialDraftApplicationError,
    EditorialDraftContent,
    EditorialDraftService,
)
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionService,
)
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
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
from cti_app.application.production_editorial_enrichment import (
    ProductionEditorialEnrichmentService,
)
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService, SynthesisService
from cti_app.application.production_synthesis import ProductionSynthesisService
from cti_app.config import get_settings
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from cti_app.domain.editorial_workspace import EditorialWorkspaceError
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler

_MAX_DRAFT_REQUEST_BYTES = MAX_DRAFT_BYTES * 6 + 4_096
logger = logging.getLogger(__name__)


class EditorialWorkspaceRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def sanitized_handler(request: Request) -> Response:
            is_accept = self.path_format.endswith("/accept")
            is_draft = self.path_format.endswith("/draft")
            if is_accept:
                try:
                    enabled = get_settings().editorial_proposal_accept_enabled
                except Exception:
                    enabled = False
                if not enabled:
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "detail": {
                                "code": "accept_not_enabled",
                                "message": "L'acceptation des propositions est désactivée.",
                            }
                        },
                    )
            try:
                response = await original_handler(request)
            except HTTPException as error:
                if not is_draft:
                    raise
                response = JSONResponse(
                    status_code=error.status_code,
                    content={"detail": jsonable_encoder(error.detail)},
                    headers=dict(error.headers or {}),
                )
            except RequestValidationError as error:
                if is_accept:
                    return JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={
                            "detail": {
                                "code": "accept_request_invalid",
                                "message": "La requête d'acceptation est invalide.",
                            }
                        },
                    )
                if is_draft:
                    response = JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={
                            "detail": {
                                "code": "draft_request_invalid",
                                "message": "La requête de brouillon est invalide.",
                            }
                        },
                    )
                else:
                    details = [
                        {
                            "loc": list(item.get("loc", ())),
                            "type": str(item.get("type", "value_error")),
                            "msg": str(item.get("msg", "Invalid value")),
                        }
                        for item in error.errors()
                    ]
                    response = JSONResponse(status_code=422, content={"detail": details})
            except Exception as error:
                if not is_draft:
                    raise
                logger.warning(
                    "draft_route_exception class=%s subject_id=%s occurrence_id=%s",
                    type(error).__name__,
                    request.path_params.get("subject_id", "unknown"),
                    request.path_params.get("occurrence_id", "unknown"),
                )
                response = JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={
                        "detail": {
                            "code": "draft_storage_unavailable",
                            "message": _ERROR_MESSAGES["draft_storage_unavailable"],
                        }
                    },
                )

            if is_draft:
                response.headers["Cache-Control"] = "no-store"
            return response

        return sanitized_handler


router = APIRouter(prefix="/api", tags=["editorial-workspace"], route_class=EditorialWorkspaceRoute)


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
    impact_status: Literal["none", "pending", "running", "superseded", "up_to_date", "failed"]
    pdf_status: Literal["CURRENT", "STALE"]
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
    base_text: str | None
    patch_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    patch: dict[str, object] | None
    diagnostics: dict[str, object]
    created_at: str


class EditorialProposalRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_state_version: int = Field(ge=1)
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EditorialProposalDecisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    state: Literal["rejected"]
    state_version: int


class EditorialProposalAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_state_version: int = Field(ge=1)
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EditorialProposalAcceptView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    state: Literal["accepted"]
    state_version: int
    artifact_id: UUID
    artifact_version: int
    snapshot_id: UUID
    impact_id: UUID
    impact_state: str


class EditorialDraftPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    text: str = Field(max_length=MAX_DRAFT_BYTES)


class EditorialDraftView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    subject_id: UUID
    production_run_id: UUID
    occurrence_id: UUID
    base_snapshot_id: UUID
    base_artifact_id: UUID
    base_artifact_version: int
    base_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int
    text: str
    is_stale: bool
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


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
    "proposal_operation_unsupported": (
        "Aucune opération de proposition sûre n’est disponible pour ce bloc."  # noqa: RUF001
    ),
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
    "proposal_base_unavailable": "Le texte de référence de cette proposition est indisponible.",
    "proposal_base_hash_mismatch": "Le texte de référence ne correspond plus à son empreinte.",
    "proposal_state_conflict": "La proposition a changé. Actualisez-la avant de décider.",
    "proposal_base_stale": (
        "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. "  # noqa: RUF001
        "Actualisez le document, puis comparez et formulez une nouvelle proposition."
    ),
    "proposal_stale": (
        "Les références de cette proposition ont changé. Rechargez l’atelier."  # noqa: RUF001
    ),
    "proposal_idempotency_conflict": "Cette clé est déjà associée à une autre décision.",
    "accept_not_enabled": "L'acceptation des propositions est désactivée.",
    "editorial_proposal_accept_unavailable": (
        "L'acceptation des propositions est temporairement indisponible."
    ),
    "editorial_thread_conversation_missing": "La conversation de ce fil est indisponible.",
    "editorial_thread_job_missing": "La génération de ce message est indisponible.",
    "editorial_thread_session_not_lost": (
        "La session actuelle est encore disponible. Aucune nouvelle discussion n’a été créée."  # noqa: RUF001
    ),
    "session_lost": "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre.",
    "editorial_reconciliation_invalid": "La requête de réconciliation est invalide.",
    "draft_not_found": "Brouillon introuvable.",
    "draft_expired": "Ce brouillon a expiré. Rechargez le document.",
    "draft_version_conflict": "Le brouillon a changé. Rechargez-le avant de continuer.",
    "draft_base_stale": "Le document a changé depuis ce brouillon. Rechargez l’atelier.",  # noqa: RUF001
    "draft_retention_unconfigured": "La conservation des brouillons n’est pas configurée.",  # noqa: RUF001
    "draft_storage_unavailable": "Le stockage du brouillon est temporairement indisponible.",
    "draft_cleanup_unavailable": "La purge du brouillon est temporairement indisponible.",
    "draft_text_too_large": "Le brouillon dépasse la taille autorisée.",
    "draft_if_match_required": "Une version de brouillon est requise pour cette modification.",
    "draft_if_match_invalid": "La version de brouillon est invalide.",
    "draft_request_invalid": "La requête de brouillon est invalide.",
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


def _draft_service(request: Request) -> EditorialDraftService:
    blob_store = getattr(request.app.state, "blob_store", None)
    if blob_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "draft_storage_unavailable",
                "message": _ERROR_MESSAGES["draft_storage_unavailable"],
            },
        )
    try:
        retention_days = get_settings().editorial_draft_retention_days
    except Exception:
        retention_days = None
    return EditorialDraftService(
        uow_factory=request.app.state.uow_factory,
        workspace_service=_service(request),
        blob_store=blob_store,
        retention_days=retention_days,
    )


def _editorial_draft_http_error(error: EditorialDraftApplicationError) -> HTTPException:
    code = error.code
    response_status = (
        status.HTTP_404_NOT_FOUND
        if code == "draft_not_found"
        else status.HTTP_410_GONE
        if code == "draft_expired"
        else status.HTTP_413_CONTENT_TOO_LARGE
        if code == "draft_text_too_large"
        else status.HTTP_428_PRECONDITION_REQUIRED
        if code == "draft_if_match_required"
        else status.HTTP_400_BAD_REQUEST
        if code == "draft_if_match_invalid"
        else status.HTTP_422_UNPROCESSABLE_CONTENT
        if code == "draft_request_invalid"
        else status.HTTP_503_SERVICE_UNAVAILABLE
        if code
        in {
            "draft_retention_unconfigured",
            "draft_storage_unavailable",
            "draft_cleanup_unavailable",
        }
        else status.HTTP_409_CONFLICT
    )
    detail: dict[str, object] = {
        "code": code,
        "message": _ERROR_MESSAGES.get(code, "Le brouillon est indisponible."),
    }
    if error.expires_at is not None:
        detail["expires_at"] = error.expires_at
    return HTTPException(
        status_code=response_status,
        detail=detail,
    )


def _draft_view(content: EditorialDraftContent) -> EditorialDraftView:
    draft = content.draft
    return EditorialDraftView(
        id=draft.id,
        subject_id=draft.subject_id,
        production_run_id=draft.production_run_id,
        occurrence_id=draft.occurrence_id,
        base_snapshot_id=draft.base_snapshot_id,
        base_artifact_id=draft.base_artifact_id,
        base_artifact_version=draft.base_artifact_version,
        base_canonical_sha256=draft.base_canonical_sha256,
        text_sha256=draft.text_sha256,
        version=draft.version,
        text=content.text,
        is_stale=content.is_stale,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        expires_at=draft.expires_at,
    )


def _draft_expected_version(if_match: str | None) -> int:
    if if_match is None:
        raise _editorial_draft_http_error(EditorialDraftApplicationError("draft_if_match_required"))
    match = re.fullmatch(r'"(0|[1-9][0-9]*)"', if_match.strip())
    if match is None:
        raise _editorial_draft_http_error(EditorialDraftApplicationError("draft_if_match_invalid"))
    return int(match.group(1))


async def _read_draft_put_payload(request: Request) -> EditorialDraftPutRequest:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise _editorial_draft_http_error(
                EditorialDraftApplicationError("draft_request_invalid")
            ) from None
        if declared_length < 0:
            raise _editorial_draft_http_error(
                EditorialDraftApplicationError("draft_request_invalid")
            )
        if declared_length > _MAX_DRAFT_REQUEST_BYTES:
            raise _editorial_draft_http_error(
                EditorialDraftApplicationError("draft_text_too_large")
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_DRAFT_REQUEST_BYTES:
            raise _editorial_draft_http_error(
                EditorialDraftApplicationError("draft_text_too_large")
            )
        body.extend(chunk)
    try:
        return EditorialDraftPutRequest.model_validate_json(bytes(body))
    except ValidationError:
        raise _editorial_draft_http_error(
            EditorialDraftApplicationError("draft_request_invalid")
        ) from None


async def _purge_decided_server_draft(
    request: Request, subject_id: UUID, occurrence_id: UUID
) -> None:
    # The draft store is wired atomically with the API's blob catalog. A test
    # or reduced application without that feature cannot contain server drafts.
    if getattr(request.app.state, "blob_store", None) is None:
        return
    try:
        await _draft_service(request).purge(subject_id, occurrence_id)
    except Exception as error:
        # Canonical decisions are already committed. Draft cleanup is
        # best-effort here; expiration sweep provides eventual row cleanup.
        logger.warning(
            "draft_purge_exception class=%s subject_id=%s occurrence_id=%s",
            type(error).__name__,
            subject_id,
            occurrence_id,
        )
        return


def _proposal_adoption_service(request: Request) -> EditorialProposalAdoptionService:
    artifact_store = getattr(request.app.state, "production_artifact_store", None)
    blob_store = getattr(request.app.state, "blob_store", None)
    model_gateway = getattr(request.app.state, "model_gateway", None)
    render_service = getattr(request.app.state, "publication_render_service", None)
    uow_factory = getattr(request.app.state, "uow_factory", None)
    if any(
        item is None
        for item in (artifact_store, blob_store, model_gateway, render_service, uow_factory)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "editorial_proposal_accept_unavailable",
                "message": _ERROR_MESSAGES["editorial_proposal_accept_unavailable"],
            },
        )

    artifact_store = cast(Any, artifact_store)
    blob_store = cast(Any, blob_store)
    model_gateway = cast(Any, model_gateway)
    render_service = cast(Any, render_service)
    uow_factory = cast(Any, uow_factory)
    synthesis_writer = SynthesisService(uow_factory, artifact_store)
    enrichment_writer = EditorialEnrichmentService(uow_factory, artifact_store)
    synthesis_loader = ProductionSynthesisService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_gateway=model_gateway,
        synthesis_service=synthesis_writer,
    )
    enrichment_loader = ProductionEditorialEnrichmentService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_gateway=model_gateway,
        editorial_enrichment_service=enrichment_writer,
        media_asset_store=getattr(request.app.state, "media_asset_store", None),
        diagram_compiler=D2DiagramCompiler(),
        chart_compiler=AnalyticChartRenderer(),
        source_media_archiver=getattr(
            getattr(request.app.state, "collection_service", None), "source_media_archiver", None
        ),
        resource_search_enabled=get_settings().production_editorial_resource_search_enabled,
    )
    enrichment_revision = ProductionEditorialEnrichmentRevisionService(
        enrichment_service=enrichment_loader,
        persistence_service=enrichment_writer,
        publication_render_service=render_service,
    )
    inputs = ProductionEditorialProposalAdoptionInputs(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        workspace_service=_service(request),
        synthesis_loader=synthesis_loader,
        enrichment_loader=enrichment_loader,
        enrichment_revision_service=enrichment_revision,
        resource_search_enabled=get_settings().production_editorial_resource_search_enabled,
    )
    return EditorialProposalAdoptionService(
        uow_factory=uow_factory,
        blob_store=blob_store,
        artifact_store=artifact_store,
        workspace_service=_service(request),
        inputs=inputs,
        synthesis_writer=synthesis_writer,
        enrichment_writer=enrichment_writer,
    )


def _proposal_impact_consumer_wired() -> bool:
    """Return whether acceptance has a registered worker consumer and dispatcher."""
    try:
        from cti_app.application.production_repair_factory import (
            create_editorial_proposal_impact_service,
        )
        from cti_app.workers.editorial_proposal_impacts import EditorialProposalImpactWorker
        from cti_app.workers.scheduler import main as run_impact_dispatch_scheduler
        from cti_app.workers.tasks import (
            dispatch_editorial_proposal_impacts,
            process_editorial_proposal_impact,
        )
    except Exception:
        return False

    return all(
        (
            callable(create_editorial_proposal_impact_service),
            callable(EditorialProposalImpactWorker),
            callable(run_impact_dispatch_scheduler),
            callable(getattr(dispatch_editorial_proposal_impacts, "send", None)),
            callable(getattr(process_editorial_proposal_impact, "send", None)),
        )
    )


def _require_proposal_writer(request: Request) -> None:
    """Fail closed unless the production unique adoption writer is constructible."""
    try:
        service = _proposal_adoption_service(request)
    except Exception:
        service = None
    if service is None or not callable(getattr(service, "adopt", None)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "propose_not_enabled",
                "message": _ERROR_MESSAGES["propose_not_enabled"],
            },
        ) from None


def _thread_service(request: Request) -> EditorialThreadService:
    from cti_app.application.editorial_discussion_context import (
        EditorialDiscussionContextBuilder,
    )

    workspace_service = _service(request)
    artifact_store = request.app.state.production_artifact_store
    blob_store = getattr(request.app.state, "blob_store", None)
    proposal_service = (
        EditorialProposalService(
            uow_factory=request.app.state.uow_factory,
            blob_store=blob_store,
            artifact_store=artifact_store,
            workspace_service=workspace_service,
        )
        if blob_store is not None
        else None
    )
    return EditorialThreadService(
        uow_factory=request.app.state.uow_factory,
        workspace_service=workspace_service,
        artifact_store=artifact_store,
        model_conversations=getattr(request.app.state, "model_conversation_service", None),
        enqueue_job=_enqueue_editorial_thread_job,
        proposal_service=proposal_service,
        discussion_context_builder=EditorialDiscussionContextBuilder(
            uow_factory=request.app.state.uow_factory,
            workspace_service=workspace_service,
            artifact_store=artifact_store,
        ),
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


@router.get(
    "/subjects/{subject_id}/editor/blocks/{occurrence_id}/draft",
    response_model=EditorialDraftView,
)
@router.get(
    "/subjects/{subject_id}/editor/threads/{occurrence_id}/draft",
    response_model=EditorialDraftView,
    include_in_schema=False,
)
async def get_editorial_draft(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    response: Response,
) -> EditorialDraftView:
    try:
        content = await _draft_service(request).get(
            subject_id=subject_id, occurrence_id=occurrence_id
        )
    except EditorialDraftApplicationError as error:
        raise _editorial_draft_http_error(error) from None
    response.headers["ETag"] = f'"{content.draft.version}"'
    return _draft_view(content)


@router.put(
    "/subjects/{subject_id}/editor/blocks/{occurrence_id}/draft",
    response_model=EditorialDraftView,
)
@router.put(
    "/subjects/{subject_id}/editor/threads/{occurrence_id}/draft",
    response_model=EditorialDraftView,
    include_in_schema=False,
)
async def put_editorial_draft(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> EditorialDraftView:
    expected_version = _draft_expected_version(if_match)
    payload = await _read_draft_put_payload(request)
    try:
        content = await _draft_service(request).save(
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=await _thread_actor_id(request),
            snapshot_id=payload.snapshot_id,
            text=payload.text,
            expected_version=expected_version,
        )
    except EditorialDraftApplicationError as error:
        raise _editorial_draft_http_error(error) from None
    response.headers["ETag"] = f'"{content.draft.version}"'
    return _draft_view(content)


@router.delete(
    "/subjects/{subject_id}/editor/blocks/{occurrence_id}/draft",
    status_code=status.HTTP_204_NO_CONTENT,
)
@router.delete(
    "/subjects/{subject_id}/editor/threads/{occurrence_id}/draft",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def delete_editorial_draft(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    expected_version = _draft_expected_version(if_match)
    try:
        await _draft_service(request).delete(
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            expected_version=expected_version,
        )
    except EditorialDraftApplicationError as error:
        raise _editorial_draft_http_error(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
        blob_store=getattr(request.app.state, "blob_store", None),
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
    "/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/reject",
    response_model=EditorialProposalDecisionView,
)
async def reject_editorial_proposal(
    subject_id: UUID,
    occurrence_id: UUID,
    proposal_id: UUID,
    request: Request,
    payload: EditorialProposalRejectRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EditorialProposalDecisionView:
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
    service = EditorialProposalService(
        uow_factory=request.app.state.uow_factory,
        blob_store=getattr(request.app.state, "blob_store", None),
        artifact_store=request.app.state.production_artifact_store,
    )
    try:
        rejected = await service.reject(
            subject_id,
            occurrence_id,
            proposal_id,
            actor_id=await _thread_actor_id(request),
            expected_state_version=payload.expected_state_version,
            patch_sha256=payload.patch_sha256,
            idempotency_key=idempotency_key,
            workspace_service=_service(request),
        )
    except EditorialProposalApplicationError as error:
        code = error.code
        response_status = (
            status.HTTP_404_NOT_FOUND
            if code == "proposal_not_found"
            else status.HTTP_400_BAD_REQUEST
            if code == "idempotency_key_invalid"
            else status.HTTP_503_SERVICE_UNAVAILABLE
            if code
            in {
                "proposal_patch_unavailable",
                "proposal_patch_hash_mismatch",
                "proposal_base_unavailable",
                "proposal_base_hash_mismatch",
            }
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=response_status,
            detail={
                "code": code,
                "message": _ERROR_MESSAGES.get(code, "La décision est indisponible."),
            },
        ) from None
    await _purge_decided_server_draft(request, subject_id, occurrence_id)
    return EditorialProposalDecisionView(
        id=rejected.id,
        state="rejected",
        state_version=rejected.state_version,
    )


@router.post(
    "/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/accept",
    response_model=EditorialProposalAcceptView,
)
async def accept_editorial_proposal(
    subject_id: UUID,
    occurrence_id: UUID,
    proposal_id: UUID,
    request: Request,
    payload: EditorialProposalAcceptRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EditorialProposalAcceptView:
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

    if not _proposal_impact_consumer_wired():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "editorial_proposal_accept_unavailable",
                "message": _ERROR_MESSAGES["editorial_proposal_accept_unavailable"],
            },
        )

    try:
        service = _proposal_adoption_service(request)
        result = await service.adopt(
            subject_id,
            occurrence_id,
            proposal_id,
            actor_id=await _thread_actor_id(request),
            expected_state_version=payload.expected_state_version,
            patch_sha256=payload.patch_sha256,
            idempotency_key=idempotency_key,
        )
    except HTTPException:
        raise
    except EditorialProposalAdoptionError as error:
        code = error.code
        if code == "proposal_not_found":
            response_status = status.HTTP_404_NOT_FOUND
        elif code == "idempotency_key_invalid":
            response_status = status.HTTP_400_BAD_REQUEST
        elif code in {
            "proposal_patch_invalid",
            "proposal_target_mismatch",
            "proposal_operation_unsupported",
        }:
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif code in {
            "proposal_patch_unavailable",
            "proposal_patch_hash_mismatch",
            "proposal_base_unavailable",
            "proposal_base_hash_mismatch",
            "proposal_policy_unavailable",
            "proposal_candidate_blob_unavailable",
            "proposal_candidate_blob_hash_mismatch",
            "proposal_acceptance_incomplete",
            "editorial_enrichment_diagram_materializer_unavailable",
            "proposal_registry_lineage_unavailable",
        }:
            response_status = status.HTTP_503_SERVICE_UNAVAILABLE
        elif code == "adoption_placement_changed":
            response_status = status.HTTP_409_CONFLICT
        else:
            response_status = status.HTTP_409_CONFLICT
        message = _ERROR_MESSAGES.get(code, "L'acceptation de cette proposition a été refusée.")
        if response_status == status.HTTP_503_SERVICE_UNAVAILABLE:
            message = _ERROR_MESSAGES["editorial_proposal_accept_unavailable"]
        raise HTTPException(
            status_code=response_status,
            detail={"code": code, "message": message},
        ) from None
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "editorial_proposal_accept_unavailable",
                "message": _ERROR_MESSAGES["editorial_proposal_accept_unavailable"],
            },
        ) from None

    await _purge_decided_server_draft(request, subject_id, occurrence_id)
    return EditorialProposalAcceptView(
        id=result.proposal.id,
        state="accepted",
        state_version=result.proposal.state_version,
        artifact_id=result.artifact.id,
        artifact_version=result.artifact.version,
        snapshot_id=result.snapshot.snapshot_id,
        impact_id=result.impact.impact_id,
        impact_state=result.impact.state.value,
    )


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
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "idempotency_key_required",
                "message": _ERROR_MESSAGES["idempotency_key_required"],
            },
        )
    request_kind = payload.get("kind")
    start_new_session = payload.get("start_new_session") is True
    if request_kind == "propose":
        _require_proposal_writer(request)
    allowed_shape = (
        set(payload) == {"kind", "start_new_session"}
        if start_new_session
        else set(payload) == {"kind", "text"}
    )
    if not allowed_shape or request_kind not in {"message", "propose"}:
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
        if request_kind == "propose":
            result = await service.send_proposal(
                subject_id,
                occurrence_id,
                text=text_value,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
        else:
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

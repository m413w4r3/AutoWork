from __future__ import annotations

import hashlib
import logging
import re
import sys
import traceback
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.responses import JSONResponse

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.editorial_drafts import (
    MAX_DRAFT_BYTES,
    EditorialDraftApplicationError,
    EditorialDraftContent,
    EditorialDraftService,
)
from cti_app.application.editorial_figure_catalog import (
    EditorialFigureCatalogError,
    EditorialFigureCatalogItem,
    EditorialFigureCatalogService,
    FigureCatalogSource,
)
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionService,
)
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
from cti_app.application.editorial_proposals import (
    EditorialBarChartDiagnosticReason,
    EditorialFigureDiagnosticReason,
    EditorialProposalApplicationError,
    EditorialProposalService,
    EditorialTableDiagnosticReason,
    editorial_manual_decision_idempotency_key,
)
from cti_app.application.editorial_thread_common import is_valid_editorial_idempotency_key
from cti_app.application.editorial_threads import (
    EditorialThreadApplicationError,
    EditorialThreadReconciliationResult,
    EditorialThreadService,
    EditorialThreadSessionStartResult,
)
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    ProductionEditorialEnrichmentService,
)
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService, SynthesisService
from cti_app.application.production_synthesis import ProductionSynthesisService
from cti_app.application.source_figure_inventory import MAX_SOURCE_FIGURES
from cti_app.config import Settings, get_settings
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_proposal import EditorialProposalConflict
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from cti_app.domain.editorial_workspace import EditorialWorkspaceError
from cti_app.domain.production_editorial_enrichment import (
    BAR_CHART_INSUFFICIENT_DATA_MESSAGE,
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    EditorialAnalyticPurposeV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
from cti_app.infrastructure.collection_runtime import create_subject_collection_service
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.figure_thumbnails import (
    MAX_FIGURE_PREVIEW_EDGE_PX,
    MAX_FIGURE_THUMBNAIL_EDGE_PX,
    FigureThumbnailError,
    PreparedFigureContent,
    figure_thumbnail_cache,
    prepare_figure_content,
)

_MAX_DRAFT_REQUEST_BYTES = MAX_DRAFT_BYTES * 6 + 4_096
_MAX_MANUAL_EDIT_REQUEST_BYTES = 256 * 1024 + 4_096
_DIAGNOSTIC_FRAME = re.compile(r"^[A-Za-z0-9_.<>-]+:[0-9]+:[A-Za-z0-9_.<>-]+$")
logger = logging.getLogger(__name__)


class EditorialTableRequestValidationError(ValueError):
    def __init__(self, reason: EditorialTableDiagnosticReason, *, frame: str) -> None:
        self.reason = reason.value
        self.frame = frame
        super().__init__(reason.value)


def _caller_frame() -> str:
    caller = sys._getframe(1)
    filename = caller.f_code.co_filename.rsplit("/", 1)[-1]
    return f"{filename}:{caller.f_lineno}:{caller.f_code.co_name}"


def _exception_frame(error: BaseException) -> str | None:
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return None
    frame = frames[-1]
    return f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"


def _manual_table_frame(error: BaseException, attached: object = None) -> str:
    if isinstance(attached, str) and _DIAGNOSTIC_FRAME.fullmatch(attached):
        return attached
    frame = _exception_frame(error)
    if frame is not None and _DIAGNOSTIC_FRAME.fullmatch(frame):
        return frame
    return _caller_frame()


def _manual_table_validator_log_code(error: BaseException | None = None) -> str:
    value = getattr(error, "validator_code", None)
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{2,}", value):
        return value
    return "none"


def _manual_table_request_diagnostics(
    error: RequestValidationError,
) -> tuple[EditorialTableDiagnosticReason, str]:
    for item in error.errors():
        context = item.get("ctx")
        validation_error = context.get("error") if isinstance(context, Mapping) else None
        if isinstance(validation_error, EditorialTableRequestValidationError):
            return (
                EditorialTableDiagnosticReason(validation_error.reason),
                _manual_table_frame(validation_error, validation_error.frame),
            )
    return (
        EditorialTableDiagnosticReason.REQUEST_SCHEMA_INVALID,
        _manual_table_frame(error),
    )


class EditorialWorkspaceRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def sanitized_handler(request: Request) -> Response:
            is_accept = self.path_format.endswith("/accept")
            is_manual_edit = self.path_format.endswith("/manual-edit")
            is_manual_table_proposal = self.path_format.endswith("/table-proposals")
            is_manual_bar_chart_proposal = self.path_format.endswith("/bar-chart-proposals")
            is_manual_source_figure_proposal = self.path_format.endswith("/figure-proposals")
            is_draft = self.path_format.endswith("/draft")
            is_figure_catalog = "/editor/figures" in self.path_format
            if (
                is_accept
                or is_manual_edit
                or is_manual_table_proposal
                or is_manual_bar_chart_proposal
                or is_manual_source_figure_proposal
            ):
                try:
                    enabled = get_settings().editorial_proposal_accept_enabled
                except Exception:
                    enabled = False
                if not enabled:
                    code = "accept_not_enabled" if is_accept else "manual_edit_unavailable"
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={
                            "detail": {
                                "code": code,
                                "message": _ERROR_MESSAGES.get(
                                    code, "L'acceptation des propositions est désactivée."
                                ),
                            }
                        },
                    )
            if (
                is_manual_edit
                or is_manual_table_proposal
                or is_manual_bar_chart_proposal
                or is_manual_source_figure_proposal
            ):
                raw_length = request.headers.get("content-length")
                if raw_length is not None:
                    try:
                        if int(raw_length) > _MAX_MANUAL_EDIT_REQUEST_BYTES:
                            return _manual_edit_too_large_response()
                    except ValueError:
                        return JSONResponse(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            content={
                                "detail": {
                                    "code": "manual_edit_request_invalid",
                                    "message": _ERROR_MESSAGES["manual_edit_request_invalid"],
                                }
                            },
                        )
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > _MAX_MANUAL_EDIT_REQUEST_BYTES:
                        return _manual_edit_too_large_response()
                    body.extend(chunk)
                cast(Any, request)._body = bytes(body)
            try:
                response = await original_handler(request)
            except HTTPException as error:
                if (
                    is_figure_catalog or is_manual_source_figure_proposal
                ) and error.status_code >= 500:
                    logger.warning(
                        "figure_proposal_failed reason=%s frame=%s exc_type=%s",
                        (
                            "dependency_unavailable"
                            if error.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
                            else "http_exception"
                        ),
                        _exception_frame(error) or _caller_frame(),
                        type(error).__name__,
                    )
                if not is_draft and not is_figure_catalog:
                    raise
                response = JSONResponse(
                    status_code=error.status_code,
                    content={"detail": jsonable_encoder(error.detail)},
                    headers=dict(error.headers or {}),
                )
            except RequestValidationError as error:
                if is_figure_catalog:
                    response = JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={
                            "detail": {
                                "code": "figure_catalog_request_invalid",
                                "message": "Les filtres de la galerie sont invalides.",
                            }
                        },
                    )
                elif is_accept or is_manual_edit:
                    code = "accept_request_invalid" if is_accept else "manual_edit_request_invalid"
                    message = (
                        "La requête d'acceptation est invalide."
                        if is_accept
                        else "La requête de modification est invalide."
                    )
                    return JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={
                            "detail": {
                                "code": code,
                                "message": message,
                            }
                        },
                    )
                elif is_manual_table_proposal:
                    reason, frame = _manual_table_request_diagnostics(error)
                    logger.warning(
                        "manual_table_proposal_rejected code=%s reason=%s validator_code=%s "
                        "frame=%s "
                        "subject_id=%s occurrence_id=%s",
                        "manual_table_request_invalid",
                        reason.value,
                        "none",
                        frame,
                        request.path_params.get("subject_id", "unknown"),
                        request.path_params.get("occurrence_id", "unknown"),
                    )
                    response = JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={
                            "detail": {
                                "code": "manual_table_request_invalid",
                                "message": _ERROR_MESSAGES["manual_table_request_invalid"],
                            }
                        },
                    )
                elif is_manual_bar_chart_proposal:
                    logger.warning(
                        "manual_bar_chart_proposal_rejected code=%s reason=%s "
                        "validator_code=%s frame=%s subject_id=%s occurrence_id=%s",
                        "manual_bar_chart_request_invalid",
                        EditorialBarChartDiagnosticReason.REQUEST_INVALID.value,
                        "bar_chart_request_schema_invalid",
                        _caller_frame(),
                        request.path_params.get("subject_id", "unknown"),
                        request.path_params.get("occurrence_id", "unknown"),
                    )
                    response = JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={
                            "detail": {
                                "code": "manual_bar_chart_request_invalid",
                                "message": _ERROR_MESSAGES["manual_bar_chart_request_invalid"],
                            }
                        },
                    )
                elif is_manual_source_figure_proposal:
                    logger.warning(
                        "manual_source_figure_proposal_rejected code=%s reason=%s "
                        "validator_code=%s exc_type=%s frame=%s subject_id=%s occurrence_id=%s",
                        "manual_source_figure_request_invalid",
                        EditorialFigureDiagnosticReason.REQUEST_INVALID.value,
                        "figure_request_schema_invalid",
                        type(error).__name__,
                        _exception_frame(error) or _caller_frame(),
                        request.path_params.get("subject_id", "unknown"),
                        request.path_params.get("occurrence_id", "unknown"),
                    )
                    response = JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={
                            "detail": {
                                "code": "manual_source_figure_request_invalid",
                                "message": _ERROR_MESSAGES["manual_source_figure_request_invalid"],
                            }
                        },
                    )
                elif is_draft:
                    response = JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content={
                            "detail": {
                                "code": "draft_request_invalid",
                                "message": "La requête de brouillon est invalide.",
                            }
                        },
                    )
                elif not is_figure_catalog:
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
                if is_figure_catalog or is_manual_source_figure_proposal:
                    logger.error(
                        "figure_proposal_failed reason=%s frame=%s exc_type=%s",
                        "unexpected_exception",
                        _exception_frame(error) or _caller_frame(),
                        type(error).__name__,
                    )
                    raise
                elif is_manual_edit:
                    return JSONResponse(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        content={
                            "detail": {
                                "code": "manual_edit_unavailable",
                                "message": _ERROR_MESSAGES["manual_edit_unavailable"],
                            }
                        },
                    )
                elif not is_draft:
                    raise
                else:
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
            if is_figure_catalog:
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["X-Content-Type-Options"] = "nosniff"
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


class EditorialWorkspaceTableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str


class EditorialWorkspaceTableRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cells: list[str]
    evidence_refs: list[dict[str, object]]


class EditorialWorkspaceTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: EnrichmentTableKind
    title: str
    caption: str | None
    columns: list[EditorialWorkspaceTableColumn]
    rows: list[EditorialWorkspaceTableRow]
    evidence_refs: list[dict[str, object]]
    anchor_block_id: UUID | None
    placement: Literal["after_paragraph", "after_lead", "after_timeline", "after_section", "end"]


class EditorialWorkspaceBarChartPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    evidence_refs: list[dict[str, object]]


class EditorialWorkspaceBarChart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: Literal["bar"]
    title: str
    caption: str | None
    legend: list[str]
    category_axis_label: str
    value_axis_label: str
    unit: str
    points: list[EditorialWorkspaceBarChartPoint]
    anchor_block_id: UUID


class EditorialWorkspaceFigure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    caption: str
    source_url: str
    source: str | None
    provenance: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str
    byte_size: int = Field(gt=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    status: Literal["proposed", "included", "excluded"]
    order: int | None = Field(default=None, ge=0)
    anchor_block_id: UUID | None
    placement: Literal["after_paragraph", "after_lead", "after_timeline", "after_section", "end"]
    page: int | None = Field(default=None, ge=1)
    section: str | None


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
    table: EditorialWorkspaceTable | None = None
    chart: EditorialWorkspaceBarChart | None = None
    figure: EditorialWorkspaceFigure | None = None
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


class EditorialFigureSourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    title: str | None
    url: str | None


class EditorialFigureCatalogItemView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    figure_id: UUID
    original_name: str
    original_caption: str | None
    alt_text: str | None
    source: EditorialFigureSourceView
    page: int | None
    section: str | None
    context_before: str | None
    context_after: str | None
    width: int | None
    height: int | None
    mime_type: str | None
    status: Literal[
        "available",
        "already_integrated",
        "not_selected",
        "ineligible",
        "file_unavailable",
        "unverified",
    ]
    selectable: bool
    exclusion_reason: str | None
    already_used: bool


class EditorialFigureCatalogPageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[EditorialFigureCatalogItemView]
    sources: list[EditorialFigureSourceView]
    total: int
    available_count: int
    offset: int
    limit: int


class EditorialFigurePreviewView(EditorialFigureCatalogItemView):
    preview_image_url: str


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
    origin: Literal["bridge_draft", "manual_patch"]
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


class EditorialManualEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    operation: Literal["rewrite_title", "rewrite_paragraph"]
    text: str = Field(min_length=1)


class EditorialTableEvidenceReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_document_id: UUID
    kind: EvidenceKind
    evidence_key: str = Field(pattern=r"^[0-9a-f]{64}$")

    def to_domain(self) -> ExtractionEvidenceRefV1:
        return ExtractionEvidenceRefV1(
            source_document_id=self.source_document_id,
            kind=self.kind,
            evidence_key=self.evidence_key,
        )


class EditorialTableColumnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=500)


class EditorialTableRowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cells: list[str] = Field(min_length=2, max_length=64)
    evidence_refs: list[EditorialTableEvidenceReferenceRequest] = Field(min_length=1, max_length=8)


class EditorialTablePurposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    available_data: str = Field(min_length=1, max_length=1000)
    comprehension_gain: str = Field(min_length=1, max_length=1000)
    scope: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[EditorialTableEvidenceReferenceRequest] = Field(min_length=1)
    knowledge_limits: str = Field(min_length=1, max_length=1000)
    placement_reason: str = Field(min_length=1, max_length=1000)


class EditorialTableDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    kind: EnrichmentTableKind
    title: str = Field(min_length=1, max_length=1000)
    caption: str | None = Field(default=None, max_length=2000)
    columns: list[EditorialTableColumnRequest] = Field(min_length=2, max_length=64)
    rows: list[EditorialTableRowRequest] = Field(min_length=1, max_length=1000)
    purpose: EditorialTablePurposeRequest

    @model_validator(mode="after")
    def _row_widths_match_columns(self) -> EditorialTableDraftRequest:
        if any(len(row.cells) != len(self.columns) for row in self.rows):
            raise EditorialTableRequestValidationError(
                EditorialTableDiagnosticReason.ROW_WIDTH_MISMATCH,
                frame=_caller_frame(),
            )
        return self


class EditorialManualTableProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    anchor_block_id: UUID
    table: EditorialTableDraftRequest


class EditorialManualSourceFigureSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    figure_id: UUID
    caption: str = Field(min_length=1, max_length=500)
    order: int = Field(ge=0, le=MAX_SOURCE_FIGURES - 1)


class EditorialManualSourceFigureProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    anchor_block_id: UUID
    figures: list[EditorialManualSourceFigureSelectionRequest] = Field(
        min_length=1, max_length=MAX_SOURCE_FIGURES
    )

    @model_validator(mode="after")
    def _selections_are_unique_and_ordered(self) -> EditorialManualSourceFigureProposalRequest:
        if len({item.figure_id for item in self.figures}) != len(self.figures):
            raise ValueError("Figure catalogue identities must be unique")
        if [item.order for item in self.figures] != list(range(len(self.figures))):
            raise ValueError("Figure order must be contiguous")
        return self


class EditorialBarChartPointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=500)
    series: str = Field(min_length=1, max_length=120)
    value: Decimal
    evidence_refs: list[EditorialTableEvidenceReferenceRequest] = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _bounded_finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("Bar chart values must be finite")
        normalized = value.normalize() if value else Decimal(0)
        if len(normalized.as_tuple().digits) > 18 or abs(normalized.adjusted()) > 12:
            raise ValueError("Bar chart value exceeds the supported numeric bounds")
        return value


class EditorialBarChartDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=1000)
    caption: str | None = Field(default=None, max_length=2000)
    category_axis_label: str = Field(min_length=1, max_length=120)
    value_axis_label: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=120)
    points: list[EditorialBarChartPointRequest] = Field(min_length=2, max_length=24)
    purpose: EditorialTablePurposeRequest

    @model_validator(mode="after")
    def _bar_chart_shape(self) -> EditorialBarChartDraftRequest:
        if any(not point.value.is_finite() for point in self.points):
            raise ValueError("Bar chart values must be finite")
        if len({point.series for point in self.points}) > 6:
            raise ValueError("Bar chart series limit exceeded")
        if len({point.label.casefold() for point in self.points}) != len(self.points):
            raise ValueError("Bar chart labels must be unique")
        if len(self.purpose.evidence_refs) > 24:
            raise ValueError("Bar chart purpose evidence limit exceeded")
        return self


class EditorialManualBarChartProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    anchor_block_id: UUID
    chart: EditorialBarChartDraftRequest


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
    "manual_edit_no_change": "Le texte n’a pas changé. Aucune nouvelle version n’a été créée.",  # noqa: RUF001
    "manual_edit_stale": (
        "Le document a changé. Actualisez l’atelier, puis réappliquez votre brouillon local."  # noqa: RUF001
    ),
    "manual_edit_format_uneditable": (
        "Le format de publication ne permet pas cette modification. Actualisez l'atelier."
    ),
    "manual_edit_target_mismatch": "Le bloc cible n'est pas compatible avec cette modification.",
    "manual_edit_target_ambiguous": "L'emplacement du bloc est ambigu. Actualisez l'atelier.",
    "manual_edit_new_fact_unadmitted": (
        "Cette nouvelle valeur technique n'est pas étayée par les preuves disponibles. "
        "Modifiez le passage ou ajoutez une source autorisée."
    ),
    "manual_table_analytic_question_duplicate": (
        "La question analytique existe déjà dans l'enrichissement."
    ),
    "manual_edit_patch_invalid": "Le texte proposé ne respecte pas le format éditorial autorisé.",
    "manual_edit_request_too_large": "La requête de modification dépasse la taille autorisée.",
    "manual_edit_idempotency_conflict": (
        "Cette clé de modification est déjà associée à un autre texte. Actualisez l’atelier."  # noqa: RUF001
    ),
    "manual_edit_request_invalid": "La requête de modification est invalide.",
    "manual_table_request_invalid": "La requête de tableau est invalide.",
    "manual_bar_chart_request_invalid": "La requête de graphique à barres est invalide.",
    "manual_source_figure_request_invalid": "La sélection de figures est invalide.",
    "brief_figure_limit_exceeded": "Une brève ne peut contenir plus de trois figures intégrées.",
    "source_figure_inventory_limit_exceeded": (
        "Le nombre de figures dépasse la limite de l'inventaire archivé."
    ),
    "editorial_figure_not_eligible": "Cette figure archivée ne peut pas être intégrée.",
    "editorial_figure_not_found": "Cette figure n'est plus disponible dans l'inventaire.",
    "figure_catalog_unavailable": "La galerie de figures est temporairement indisponible.",
    "manual_bar_chart_analytic_question_duplicate": (
        "La question analytique existe déjà dans l'enrichissement."
    ),
    "bar_chart_insufficient_data": BAR_CHART_INSUFFICIENT_DATA_MESSAGE,
    "bar_chart_value_not_evidenced": BAR_CHART_INSUFFICIENT_DATA_MESSAGE,
    "manual_edit_unavailable": "La modification manuelle est temporairement indisponible.",
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


def _manual_edit_too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        content={
            "detail": {
                "code": "manual_edit_request_too_large",
                "message": _ERROR_MESSAGES["manual_edit_request_too_large"],
            }
        },
    )


def _manual_edit_capability_enabled(request: Request, payload: Mapping[str, object]) -> bool:
    try:
        if not get_settings().editorial_proposal_accept_enabled:
            return False
        if not _proposal_impact_consumer_wired():
            return False
        if payload.get("publication_kind") not in {"article", "brief"}:
            return False
        if not isinstance(payload.get("production_run_id"), str):
            return False
        adoption_service = _proposal_adoption_service(request)
        proposal_service = _manual_proposal_service(request)
        if not callable(getattr(adoption_service, "adopt", None)):
            return False
        if not callable(getattr(proposal_service, "prepare_manual_patch", None)):
            return False
        blocks = payload.get("blocks")
        if not isinstance(blocks, list):
            return False
        return any(
            isinstance(block, Mapping)
            and isinstance(block.get("text"), str)
            and block.get("before_hash") is not None
            and block.get("revision_status") == "ready"
            and (
                (
                    block.get("kind") == "title"
                    and "rewrite_title" in block.get("allowed_operations", [])
                )
                or (
                    block.get("kind") in {"lead_paragraph", "paragraph"}
                    and "rewrite_paragraph" in block.get("allowed_operations", [])
                )
            )
            for block in blocks
        )
    except Exception:
        return False


def _service(request: Request) -> EditorialWorkspaceService:
    artifact_store = _production_artifact_store_for_request(request)
    if artifact_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "editorial_snapshot_storage_unavailable",
                "message": "Le stockage des documents éditoriaux est indisponible.",
            },
        )
    figure_catalog_service: EditorialFigureCatalogService | None = None
    if (
        isinstance(getattr(request.app.state, "settings", None), Settings)
        or getattr(request.app.state, "collection_service", None) is not None
    ):
        figure_catalog_service = _figure_catalog_service(
            request,
            artifact_store=artifact_store,
            uow_factory=request.app.state.uow_factory,
        )
    return EditorialWorkspaceService(
        uow_factory=request.app.state.uow_factory,
        artifact_store=artifact_store,
        figure_catalog_service=figure_catalog_service,
    )


def _production_artifact_store_for_request(request: Request) -> ProductionArtifactStore | None:
    configured = getattr(request.app.state, "production_artifact_store", None)
    if configured is not None:
        return cast(ProductionArtifactStore, configured)
    blob_store = getattr(request.app.state, "blob_store", None)
    uow_factory = getattr(request.app.state, "uow_factory", None)
    if blob_store is None or uow_factory is None:
        return None
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    request.app.state.production_artifact_store = artifact_store
    return artifact_store


def _collection_service_for_request(request: Request) -> Any | None:
    configured = getattr(request.app.state, "collection_service", None)
    if configured is not None:
        return configured
    blob_store = getattr(request.app.state, "blob_store", None)
    uow_factory = getattr(request.app.state, "uow_factory", None)
    if blob_store is None or uow_factory is None:
        return None
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        settings = get_settings()
    collection_service = create_subject_collection_service(
        settings=settings,
        uow_factory=uow_factory,
        blob_store=blob_store,
        model_gateway=getattr(request.app.state, "model_gateway", None),
    )
    request.app.state.collection_service = collection_service
    return collection_service


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


def _media_asset_store_for_request(
    request: Request, *, artifact_store: Any, uow_factory: Any
) -> MediaAssetStore:
    configured = getattr(request.app.state, "media_asset_store", None)
    if configured is not None:
        return cast(MediaAssetStore, configured)
    return MediaAssetStore(artifact_store, uow_factory)


def _proposal_adoption_service(request: Request) -> EditorialProposalAdoptionService:
    artifact_store = _production_artifact_store_for_request(request)
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
        media_asset_store=_media_asset_store_for_request(
            request, artifact_store=artifact_store, uow_factory=uow_factory
        ),
        diagram_compiler=D2DiagramCompiler(),
        chart_compiler=AnalyticChartRenderer(),
        source_media_archiver=getattr(
            _collection_service_for_request(request), "source_media_archiver", None
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


def _manual_proposal_service(request: Request) -> EditorialProposalService:
    uow_factory = getattr(request.app.state, "uow_factory", None)
    artifact_store = _production_artifact_store_for_request(request)
    blob_store = getattr(request.app.state, "blob_store", None)
    if uow_factory is None or artifact_store is None or blob_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "manual_edit_unavailable",
                "message": _ERROR_MESSAGES["manual_edit_unavailable"],
            },
        )
    from cti_app.application.editorial_discussion_context import (
        EditorialDiscussionContextBuilder,
    )

    workspace_service = _service(request)
    return EditorialProposalService(
        uow_factory=uow_factory,
        blob_store=blob_store,
        artifact_store=artifact_store,
        workspace_service=workspace_service,
        discussion_context_builder=EditorialDiscussionContextBuilder(
            uow_factory=uow_factory,
            workspace_service=workspace_service,
            artifact_store=artifact_store,
        ),
        chart_compiler=AnalyticChartRenderer(),
        figure_catalog_service=_figure_catalog_service(
            request,
            artifact_store=artifact_store,
            uow_factory=uow_factory,
        ),
        media_asset_store=_media_asset_store_for_request(
            request, artifact_store=artifact_store, uow_factory=uow_factory
        ),
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
            chart_compiler=AnalyticChartRenderer(),
            media_asset_store=_media_asset_store_for_request(
                request,
                artifact_store=artifact_store,
                uow_factory=request.app.state.uow_factory,
            ),
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
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, dict):
        capabilities["manual_edit"] = _manual_edit_capability_enabled(request, payload)
    return EditorialWorkspaceSnapshotView.model_validate(payload)


_FIGURE_STATUS_FILTERS = Literal[
    "available",
    "already_integrated",
    "not_selected",
    "ineligible",
    "file_unavailable",
    "unverified",
]


def _figure_catalog_service(
    request: Request,
    *,
    artifact_store: ProductionArtifactStore | None = None,
    uow_factory: Any | None = None,
) -> EditorialFigureCatalogService:
    resolved_artifact_store = (
        artifact_store
        if artifact_store is not None
        else _production_artifact_store_for_request(request)
    )
    resolved_uow_factory = (
        uow_factory if uow_factory is not None else getattr(request.app.state, "uow_factory", None)
    )
    if resolved_artifact_store is None or resolved_uow_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "figure_catalog_unavailable",
                "message": "La galerie de figures est temporairement indisponible.",
            },
        )
    collection_service = _collection_service_for_request(request)
    media_archiver = getattr(collection_service, "source_media_archiver", None)
    policy_sha256 = getattr(media_archiver, "policy_sha256", None)
    return EditorialFigureCatalogService(
        uow_factory=resolved_uow_factory,
        artifact_store=resolved_artifact_store,
        media_policy_sha256=policy_sha256 if isinstance(policy_sha256, str) else None,
    )


def _figure_catalog_http_error(error: EditorialFigureCatalogError) -> HTTPException:
    if error.code == "editorial_figure_not_found":
        status_code = status.HTTP_404_NOT_FOUND
        message = "Cette figure n'est pas disponible dans cette galerie."
    elif error.code == "figure_catalog_request_invalid":
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        message = "Les filtres de la galerie sont invalides."
    elif error.code == "figure_thumbnail_unavailable":
        status_code = status.HTTP_404_NOT_FOUND
        message = "L'aperçu de cette figure n'est pas disponible."
    else:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        message = "La galerie de figures est temporairement indisponible."
    return HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": message},
    )


def _figure_source_view(source: FigureCatalogSource) -> EditorialFigureSourceView:
    return EditorialFigureSourceView.model_validate(
        {"source_id": source.source_id, "title": source.title, "url": source.url}
    )


def _figure_catalog_item_view(
    item: EditorialFigureCatalogItem,
) -> EditorialFigureCatalogItemView:
    return EditorialFigureCatalogItemView.model_validate(
        {
            "figure_id": item.figure_id,
            "original_name": item.original_name,
            "original_caption": item.original_caption,
            "alt_text": item.alt_text,
            "source": _figure_source_view(item.source),
            "page": item.page,
            "section": item.section,
            "context_before": item.context_before,
            "context_after": item.context_after,
            "width": item.width,
            "height": item.height,
            "mime_type": item.mime_type,
            "status": item.status,
            "selectable": item.selectable,
            "exclusion_reason": item.exclusion_reason,
            "already_used": item.already_used,
        }
    )


@router.get(
    "/subjects/{subject_id}/editor/figures",
    response_model=EditorialFigureCatalogPageView,
)
async def get_editorial_figure_catalog(
    subject_id: UUID,
    request: Request,
    response: Response,
    figure_status: _FIGURE_STATUS_FILTERS | None = Query(default=None, alias="status"),
    source_id: UUID | None = Query(default=None, alias="source"),
    search: str | None = Query(default=None, alias="q", max_length=128),
    offset: int = Query(default=0, ge=0, le=512),
    limit: int = Query(default=30, ge=1, le=50),
) -> EditorialFigureCatalogPageView:
    try:
        page = await _figure_catalog_service(request).catalog(
            subject_id=subject_id,
            status_filter=figure_status,
            source_filter=source_id,
            search=search,
            offset=offset,
            limit=limit,
        )
    except EditorialFigureCatalogError as error:
        raise _figure_catalog_http_error(error) from None
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return EditorialFigureCatalogPageView(
        items=[_figure_catalog_item_view(item) for item in page.items],
        sources=[_figure_source_view(source) for source in page.sources],
        total=page.total,
        available_count=page.available_count,
        offset=page.offset,
        limit=page.limit,
    )


@router.get(
    "/subjects/{subject_id}/editor/figures/{figure_id}/thumbnail",
    response_class=Response,
)
async def get_editorial_figure_thumbnail(
    subject_id: UUID, figure_id: UUID, request: Request
) -> Response:
    prepared, _item = await _read_editorial_figure(
        request,
        subject_id,
        figure_id,
        requested_size=MAX_FIGURE_THUMBNAIL_EDGE_PX,
    )
    return _figure_content_response(prepared)


@router.get(
    "/subjects/{subject_id}/editor/figures/{figure_id}/preview",
    response_model=EditorialFigurePreviewView,
)
async def get_editorial_figure_preview(
    subject_id: UUID,
    figure_id: UUID,
    request: Request,
    response: Response,
) -> EditorialFigurePreviewView:
    try:
        item = await _figure_catalog_service(request).resolve(
            subject_id=subject_id, figure_id=figure_id
        )
    except EditorialFigureCatalogError as error:
        raise _figure_catalog_http_error(error) from None
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return EditorialFigurePreviewView.model_validate(
        {
            **_figure_catalog_item_view(item).model_dump(mode="json"),
            "preview_image_url": (
                f"/api/subjects/{subject_id}/editor/figures/{figure_id}/preview/image"
            ),
        }
    )


@router.get(
    "/subjects/{subject_id}/editor/figures/{figure_id}/preview/image",
    response_class=Response,
)
async def get_editorial_figure_preview_image(
    subject_id: UUID, figure_id: UUID, request: Request
) -> Response:
    prepared, _item = await _read_editorial_figure(
        request,
        subject_id,
        figure_id,
        requested_size=MAX_FIGURE_PREVIEW_EDGE_PX,
    )
    return _figure_content_response(prepared)


def _figure_content_response(prepared: PreparedFigureContent) -> Response:
    return Response(
        content=prepared.content,
        headers={
            "Content-Type": prepared.media_type,
            "Content-Security-Policy": prepared.content_security_policy,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _read_editorial_figure(
    request: Request,
    subject_id: UUID,
    figure_id: UUID,
    *,
    requested_size: int,
) -> tuple[PreparedFigureContent, EditorialFigureCatalogItem]:
    service = _figure_catalog_service(request)
    try:
        item = await service.resolve(subject_id=subject_id, figure_id=figure_id)
    except EditorialFigureCatalogError as error:
        raise _figure_catalog_http_error(error) from None
    if item._blob_id is None or item._sha256 is None or item._byte_size is None:
        raise _figure_catalog_http_error(
            EditorialFigureCatalogError("figure_thumbnail_unavailable")
        ) from None
    async with request.app.state.uow_factory() as uow:
        subject = await uow.subjects.get(subject_id)
        run = await uow.production_runs.get_current_for_subject(subject_id)
        source_document = await uow.source_documents.get(item._source_document_id)
        if (
            subject is None
            or run is None
            or run.id != item._run_id
            or source_document.subject_id != subject_id
        ):
            raise _figure_catalog_http_error(
                EditorialFigureCatalogError("editorial_figure_not_found")
            ) from None
        if source_document.source_collection_id is not None:
            collection = await uow.source_collections.get(source_document.source_collection_id)
            if (
                collection is None
                or collection.subject_id != subject_id
                or collection.state
                not in {
                    CollectionState.ARCHIVED,
                    CollectionState.EXTRACTED,
                    CollectionState.COMPLETED,
                }
                or (
                    collection.source_document_id is not None
                    and collection.source_document_id != source_document.id
                )
            ):
                raise _figure_catalog_http_error(
                    EditorialFigureCatalogError("editorial_figure_not_found")
                ) from None
        blob = await uow.blobs.get(item._blob_id)
        if (
            blob is None
            or blob.descriptor.sha256 != item._sha256
            or blob.descriptor.size != item._byte_size
            or blob.descriptor.size > 5 * 1024 * 1024
        ):
            raise _figure_catalog_http_error(
                EditorialFigureCatalogError("figure_thumbnail_unavailable")
            ) from None
    mime_type = item.mime_type or ""
    prepared = figure_thumbnail_cache.get(item._sha256, requested_size, mime_type)
    if prepared is None:
        content = await request.app.state.production_artifact_store.read_bytes(
            item._blob_id, max_bytes=5 * 1024 * 1024
        )
        if len(content) != item._byte_size or hashlib.sha256(content).hexdigest() != item._sha256:
            raise _figure_catalog_http_error(
                EditorialFigureCatalogError("figure_thumbnail_unavailable")
            ) from None
        try:
            prepared = prepare_figure_content(content, mime_type)
        except FigureThumbnailError as error:
            raise _figure_catalog_http_error(
                EditorialFigureCatalogError("figure_thumbnail_unavailable")
            ) from error
        figure_thumbnail_cache.put(item._sha256, requested_size, mime_type, prepared)
    return prepared, item


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
        logger.warning(
            "proposal_decision_rejected code=%s reason=%s frame=%s proposal_id=%s",
            code,
            error.reason or "not_applicable",
            error.frame or "unavailable:0:unknown",
            proposal_id,
        )
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
    "/subjects/{subject_id}/editor/blocks/{occurrence_id}/table-proposals",
    response_model=EditorialProposalView,
)
async def prepare_manual_editorial_table_proposal(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    payload: EditorialManualTableProposalRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EditorialProposalView:
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
    if payload.anchor_block_id != occurrence_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "editorial_anchor_stale",
                "message": _ERROR_MESSAGES["editorial_anchor_stale"],
            },
        )
    if not _proposal_impact_consumer_wired():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "manual_edit_unavailable",
                "message": _ERROR_MESSAGES["manual_edit_unavailable"],
            },
        )

    from cti_app.domain.editorial_proposal import ManualPatch, ManualTablePatch

    table_input = payload.table
    purpose_input = table_input.purpose
    try:
        table = TableSpecV1(
            key=table_input.key,
            kind=table_input.kind,
            title=table_input.title,
            caption=table_input.caption,
            columns=tuple(
                TableColumnV1(column.key, column.label) for column in table_input.columns
            ),
            rows=tuple(
                TableRowV1(
                    tuple(row.cells),
                    tuple(reference.to_domain() for reference in row.evidence_refs),
                )
                for row in table_input.rows
            ),
            placement=EnrichmentPlacementV1(
                EnrichmentPlacementKind.AFTER_PARAGRAPH,
                anchor_block_id=occurrence_id,
            ),
            purpose=EditorialAnalyticPurposeV1(
                question=purpose_input.question,
                available_data=purpose_input.available_data,
                comprehension_gain=purpose_input.comprehension_gain,
                scope=purpose_input.scope,
                evidence_refs=tuple(
                    reference.to_domain() for reference in purpose_input.evidence_refs
                ),
                knowledge_limits=purpose_input.knowledge_limits,
                placement_reason=purpose_input.placement_reason,
            ),
        )
        service = _manual_proposal_service(request)
        actor_id = await _thread_actor_id(request)
        proposal = await service.prepare_manual_patch(
            subject_id=subject_id,
            snapshot_id=payload.snapshot_id,
            occurrence_id=occurrence_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            patch=ManualPatch(
                typed_patch=ManualTablePatch(anchor_block_id=occurrence_id, table=table)
            ),
        )
        if proposal.patch_sha256 is None:
            raise EditorialProposalApplicationError("proposal_patch_invalid")
        view = await service.inspect(
            subject_id,
            occurrence_id,
            proposal.id,
            workspace_service=_service(request),
        )
        return EditorialProposalView.model_validate(view, from_attributes=True)
    except HTTPException:
        raise
    except EditorialProposalConflict as error:
        frame = _manual_table_frame(error)
        logger.warning(
            "manual_table_proposal_rejected code=%s reason=%s validator_code=%s frame=%s "
            "subject_id=%s occurrence_id=%s",
            error.code,
            EditorialTableDiagnosticReason.IDEMPOTENCY_CONFLICT.value,
            _manual_table_validator_log_code(error),
            frame,
            subject_id,
            occurrence_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "proposal_idempotency_conflict",
                "message": _ERROR_MESSAGES["proposal_idempotency_conflict"],
            },
        ) from error
    except (EditorialProposalApplicationError, EditorialWorkspaceError, ValueError) as error:
        raw_code = getattr(error, "code", None)
        code = (
            raw_code
            if isinstance(raw_code, str) and re.fullmatch(r"[a-z][a-z0-9_]*", raw_code)
            else "manual_edit_patch_invalid"
        )
        raw_reason = getattr(error, "reason", None)
        try:
            if not isinstance(raw_reason, str):
                raise ValueError
            reason = EditorialTableDiagnosticReason(raw_reason)
        except ValueError:
            reason = EditorialTableDiagnosticReason.VALIDATOR_REJECTED
        frame = _manual_table_frame(error, getattr(error, "frame", None))
        logger.warning(
            "manual_table_proposal_rejected code=%s reason=%s validator_code=%s frame=%s "
            "subject_id=%s occurrence_id=%s",
            code,
            reason.value,
            _manual_table_validator_log_code(error),
            frame,
            subject_id,
            occurrence_id,
        )
        if code in {"editorial_anchor_stale", "proposal_base_stale", "editorial_snapshot_stale"}:
            response_status = status.HTTP_409_CONFLICT
        elif code in {
            "manual_edit_new_fact_unadmitted",
            "manual_edit_patch_invalid",
            "manual_table_analytic_question_duplicate",
            "proposal_target_mismatch",
            "proposal_operation_unsupported",
        }:
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif code in {"proposal_base_unavailable", "proposal_workspace_unavailable"}:
            response_status = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(
            status_code=response_status,
            detail={
                "code": code,
                "message": _ERROR_MESSAGES.get(
                    code, "Le brouillon de tableau ne respecte pas le contrat éditorial."
                ),
            },
        ) from None


_MANUAL_FIGURE_VALIDATOR_CODES = frozenset(
    {
        "figure_caption_markup_forbidden",
        "brief_figure_limit_exceeded",
        "source_figure_inventory_limit_exceeded",
        "editorial_figure_not_eligible",
        "editorial_figure_not_found",
        "figure_catalog_unavailable",
        "editorial_anchor_stale",
        "proposal_base_unavailable",
        "proposal_operation_unsupported",
        "proposal_target_mismatch",
        "proposal_patch_invalid",
        "revision_wire_markup_forbidden",
    }
)


def _manual_figure_validator_log_code(error: BaseException | None) -> str:
    value = getattr(error, "validator_code", None)
    return value if isinstance(value, str) and value in _MANUAL_FIGURE_VALIDATOR_CODES else "none"


@router.post(
    "/subjects/{subject_id}/editor/blocks/{occurrence_id}/figure-proposals",
    response_model=EditorialProposalView,
)
async def prepare_manual_editorial_source_figure_proposal(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    payload: EditorialManualSourceFigureProposalRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EditorialProposalView:
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
    if payload.anchor_block_id != occurrence_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "editorial_anchor_stale",
                "message": _ERROR_MESSAGES["editorial_anchor_stale"],
            },
        )
    if not _proposal_impact_consumer_wired():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "manual_edit_unavailable",
                "message": _ERROR_MESSAGES["manual_edit_unavailable"],
            },
        )

    from cti_app.domain.editorial_proposal import (
        ManualPatch,
        ManualSourceFigurePatch,
        ManualSourceFigureSelection,
    )

    try:
        service = _manual_proposal_service(request)
        proposal = await service.prepare_manual_patch(
            subject_id=subject_id,
            snapshot_id=payload.snapshot_id,
            occurrence_id=occurrence_id,
            actor_id=await _thread_actor_id(request),
            idempotency_key=idempotency_key,
            patch=ManualPatch(
                typed_patch=ManualSourceFigurePatch(
                    anchor_block_id=occurrence_id,
                    figures=tuple(
                        ManualSourceFigureSelection(
                            figure_id=item.figure_id,
                            caption=item.caption,
                            order=item.order,
                        )
                        for item in payload.figures
                    ),
                )
            ),
        )
        if proposal.patch_sha256 is None:
            raise EditorialProposalApplicationError("proposal_patch_invalid")
        view = await service.inspect(
            subject_id,
            occurrence_id,
            proposal.id,
            workspace_service=_service(request),
        )
        return EditorialProposalView.model_validate(view, from_attributes=True)
    except HTTPException:
        raise
    except EditorialProposalConflict as error:
        logger.warning(
            "manual_source_figure_proposal_rejected code=%s reason=%s validator_code=%s "
            "exc_type=%s frame=%s subject_id=%s occurrence_id=%s",
            "proposal_idempotency_conflict",
            EditorialFigureDiagnosticReason.IDEMPOTENCY_CONFLICT.value,
            _manual_figure_validator_log_code(error),
            type(error).__name__,
            _manual_table_frame(error),
            subject_id,
            occurrence_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "proposal_idempotency_conflict",
                "message": _ERROR_MESSAGES["proposal_idempotency_conflict"],
            },
        ) from None
    except (EditorialProposalApplicationError, EditorialWorkspaceError) as error:
        raw_code = getattr(error, "code", None)
        code = (
            raw_code
            if isinstance(raw_code, str) and re.fullmatch(r"[a-z][a-z0-9_]*", raw_code)
            else "manual_edit_patch_invalid"
        )
        raw_reason = getattr(error, "reason", None)
        try:
            if not isinstance(raw_reason, str):
                raise ValueError("figure_reason_invalid")
            reason = EditorialFigureDiagnosticReason(raw_reason)
        except (TypeError, ValueError):
            reason = {
                "editorial_anchor_stale": EditorialFigureDiagnosticReason.ANCHOR_STALE,
                "editorial_figure_not_found": EditorialFigureDiagnosticReason.CATALOG_UNAVAILABLE,
                "figure_catalog_unavailable": EditorialFigureDiagnosticReason.CATALOG_UNAVAILABLE,
                "editorial_figure_not_eligible": EditorialFigureDiagnosticReason.NOT_ELIGIBLE,
                "brief_figure_limit_exceeded": EditorialFigureDiagnosticReason.BRIEF_LIMIT,
                "source_figure_inventory_limit_exceeded": (
                    EditorialFigureDiagnosticReason.INVENTORY_LIMIT
                ),
                "manual_edit_patch_invalid": EditorialFigureDiagnosticReason.MARKUP_REJECTED,
                "proposal_base_unavailable": (
                    EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE
                ),
                "proposal_workspace_unavailable": (
                    EditorialFigureDiagnosticReason.DEPENDENCIES_UNAVAILABLE
                ),
            }.get(code, EditorialFigureDiagnosticReason.VALIDATOR_REJECTED)
        logger.warning(
            "manual_source_figure_proposal_rejected code=%s reason=%s validator_code=%s "
            "exc_type=%s frame=%s subject_id=%s occurrence_id=%s",
            code,
            reason.value,
            _manual_figure_validator_log_code(error),
            type(error).__name__,
            _manual_table_frame(error, getattr(error, "frame", None)),
            subject_id,
            occurrence_id,
        )
        response_status = (
            status.HTTP_409_CONFLICT
            if code in {"editorial_anchor_stale", "proposal_base_stale", "editorial_snapshot_stale"}
            else status.HTTP_503_SERVICE_UNAVAILABLE
            if code
            in {
                "proposal_base_unavailable",
                "proposal_workspace_unavailable",
                "figure_catalog_unavailable",
            }
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(
            status_code=response_status,
            detail={
                "code": code,
                "message": _ERROR_MESSAGES.get(code, _ERROR_MESSAGES["manual_edit_patch_invalid"]),
            },
        ) from None


@router.post(
    "/subjects/{subject_id}/editor/blocks/{occurrence_id}/bar-chart-proposals",
    response_model=EditorialProposalView,
)
async def prepare_manual_editorial_bar_chart_proposal(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    payload: EditorialManualBarChartProposalRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EditorialProposalView:
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
    if payload.anchor_block_id != occurrence_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "editorial_anchor_stale",
                "message": _ERROR_MESSAGES["editorial_anchor_stale"],
            },
        )
    if not _proposal_impact_consumer_wired():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "manual_edit_unavailable",
                "message": _ERROR_MESSAGES["manual_edit_unavailable"],
            },
        )

    from cti_app.domain.editorial_proposal import ManualBarChartPatch, ManualPatch

    chart_input = payload.chart
    purpose_input = chart_input.purpose
    try:
        if len(chart_input.points) < 2:
            raise ValueError("bar_chart_insufficient_data")
        chart = ChartSpecV1(
            key=chart_input.key,
            kind=ChartKind.BAR,
            title=chart_input.title,
            caption=chart_input.caption,
            placement=EnrichmentPlacementV1(
                EnrichmentPlacementKind.AFTER_PARAGRAPH,
                anchor_block_id=occurrence_id,
            ),
            purpose=EditorialAnalyticPurposeV1(
                question=purpose_input.question,
                available_data=purpose_input.available_data,
                comprehension_gain=purpose_input.comprehension_gain,
                scope=purpose_input.scope,
                evidence_refs=tuple(
                    reference.to_domain() for reference in purpose_input.evidence_refs
                ),
                knowledge_limits=purpose_input.knowledge_limits,
                placement_reason=purpose_input.placement_reason,
            ),
            points=tuple(
                ChartPointV1(
                    label=point.label,
                    date=None,
                    series=point.series,
                    evidence_refs=tuple(reference.to_domain() for reference in point.evidence_refs),
                    value=point.value,
                )
                for point in chart_input.points
            ),
            category_axis_label=chart_input.category_axis_label,
            value_axis_label=chart_input.value_axis_label,
            unit=chart_input.unit,
        )
        service = _manual_proposal_service(request)
        actor_id = await _thread_actor_id(request)
        proposal = await service.prepare_manual_patch(
            subject_id=subject_id,
            snapshot_id=payload.snapshot_id,
            occurrence_id=occurrence_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            patch=ManualPatch(
                typed_patch=ManualBarChartPatch(
                    anchor_block_id=occurrence_id,
                    chart=chart,
                )
            ),
        )
        if proposal.patch_sha256 is None:
            raise EditorialProposalApplicationError("proposal_patch_invalid")
        view = await service.inspect(
            subject_id,
            occurrence_id,
            proposal.id,
            workspace_service=_service(request),
        )
        return EditorialProposalView.model_validate(view, from_attributes=True)
    except HTTPException:
        raise
    except EditorialProposalConflict as error:
        logger.warning(
            "manual_bar_chart_proposal_rejected code=%s reason=%s validator_code=%s frame=%s "
            "subject_id=%s occurrence_id=%s",
            error.code,
            EditorialBarChartDiagnosticReason.IDEMPOTENCY_CONFLICT.value,
            _manual_table_validator_log_code(error),
            _manual_table_frame(error),
            subject_id,
            occurrence_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "proposal_idempotency_conflict",
                "message": _ERROR_MESSAGES["proposal_idempotency_conflict"],
            },
        ) from error
    except (EditorialProposalApplicationError, EditorialWorkspaceError, ValueError) as error:
        raw_code = getattr(error, "code", None)
        code = (
            raw_code
            if isinstance(raw_code, str) and re.fullmatch(r"[a-z][a-z0-9_]*", raw_code)
            else "manual_edit_patch_invalid"
        )
        raw_reason = getattr(error, "reason", None)
        try:
            if not isinstance(raw_reason, str):
                raise ValueError
            reason = EditorialBarChartDiagnosticReason(raw_reason)
        except ValueError:
            reason = {
                "bar_chart_insufficient_data": (
                    EditorialBarChartDiagnosticReason.EVIDENCE_UNADMITTED
                ),
                "manual_edit_new_fact_unadmitted": (
                    EditorialBarChartDiagnosticReason.EVIDENCE_UNADMITTED
                ),
                "editorial_anchor_stale": EditorialBarChartDiagnosticReason.ANCHOR_STALE,
                "proposal_base_unavailable": (
                    EditorialBarChartDiagnosticReason.DEPENDENCIES_UNAVAILABLE
                ),
                "proposal_workspace_unavailable": (
                    EditorialBarChartDiagnosticReason.DEPENDENCIES_UNAVAILABLE
                ),
            }.get(code, EditorialBarChartDiagnosticReason.VALIDATOR_REJECTED)
        frame = _manual_table_frame(error, getattr(error, "frame", None))
        logger.warning(
            "manual_bar_chart_proposal_rejected code=%s reason=%s validator_code=%s frame=%s "
            "subject_id=%s occurrence_id=%s",
            code,
            reason.value,
            _manual_table_validator_log_code(error),
            frame,
            subject_id,
            occurrence_id,
        )
        response_status = (
            status.HTTP_409_CONFLICT
            if code in {"editorial_anchor_stale", "proposal_base_stale", "editorial_snapshot_stale"}
            else status.HTTP_503_SERVICE_UNAVAILABLE
            if code in {"proposal_base_unavailable", "proposal_workspace_unavailable"}
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        message = (
            BAR_CHART_INSUFFICIENT_DATA_MESSAGE
            if code in {"bar_chart_insufficient_data", "bar_chart_value_not_evidenced"}
            else _ERROR_MESSAGES.get(code, _ERROR_MESSAGES["manual_edit_patch_invalid"])
        )
        raise HTTPException(
            status_code=response_status,
            detail={"code": code, "message": message},
        ) from None


@router.post(
    "/subjects/{subject_id}/editor/blocks/{occurrence_id}/manual-edit",
    response_model=EditorialProposalAcceptView,
)
async def manually_edit_editorial_block(
    subject_id: UUID,
    occurrence_id: UUID,
    request: Request,
    payload: EditorialManualEditRequest,
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
                "code": "manual_edit_unavailable",
                "message": _ERROR_MESSAGES["manual_edit_unavailable"],
            },
        )

    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch
    from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation

    actor_id = await _thread_actor_id(request)
    try:
        proposal = await _manual_proposal_service(request).prepare_manual_patch(
            subject_id=subject_id,
            snapshot_id=payload.snapshot_id,
            occurrence_id=occurrence_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            patch=ManualPatch(
                typed_patch=ManualTextPatch(
                    operation=TargetedEditorialRevisionOperation(payload.operation),
                    text=payload.text,
                )
            ),
        )
        if proposal.patch_sha256 is None:
            raise EditorialProposalApplicationError("proposal_patch_invalid")
        result = await _proposal_adoption_service(request).adopt(
            subject_id,
            occurrence_id,
            proposal.id,
            actor_id=actor_id,
            expected_state_version=1,
            patch_sha256=proposal.patch_sha256,
            idempotency_key=editorial_manual_decision_idempotency_key(idempotency_key),
        )
    except HTTPException:
        raise
    except EditorialProposalConflict as error:
        logger.warning(
            "manual_edit_rejected code=%s reason=%s subject_id=%s occurrence_id=%s",
            error.code,
            "not_applicable",
            subject_id,
            occurrence_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "manual_edit_idempotency_conflict",
                "message": _ERROR_MESSAGES["manual_edit_idempotency_conflict"],
            },
        ) from error
    except EditorialProposalApplicationError as error:
        code = error.code
        logger.warning(
            "manual_edit_rejected code=%s reason=%s subject_id=%s occurrence_id=%s",
            code,
            error.reason or "not_applicable",
            subject_id,
            occurrence_id,
        )
        if code == "idempotency_key_invalid":
            response_status = status.HTTP_400_BAD_REQUEST
        elif code in {"proposal_base_stale", "proposal_state_conflict"}:
            response_status = status.HTTP_409_CONFLICT
        elif code == "publication_kind_unresolved":
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "manual_edit_format_uneditable"
        elif code == "proposal_target_mismatch":
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "manual_edit_target_mismatch"
        elif code == "proposal_target_ambiguous":
            response_status = status.HTTP_409_CONFLICT
            code = "manual_edit_target_ambiguous"
        elif code in {
            "manual_edit_no_change",
            "manual_edit_new_fact_unadmitted",
            "manual_edit_patch_invalid",
            "proposal_operation_unsupported",
        }:
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif code == "manual_edit_request_too_large":
            response_status = status.HTTP_413_CONTENT_TOO_LARGE
        elif code in {
            "proposal_workspace_unavailable",
            "proposal_base_unavailable",
            "proposal_patch_storage_unavailable",
        }:
            response_status = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            response_status = status.HTTP_409_CONFLICT
        if code in {"proposal_base_stale", "proposal_state_conflict"}:
            code = "manual_edit_stale"
        if response_status == status.HTTP_503_SERVICE_UNAVAILABLE:
            code = "manual_edit_unavailable"
        raise HTTPException(
            status_code=response_status,
            detail={
                "code": code,
                "message": _ERROR_MESSAGES.get(
                    code,
                    "La modification a été refusée. Rechargez l’atelier avant de réessayer.",  # noqa: RUF001
                ),
            },
        ) from error
    except EditorialProposalAdoptionError as error:
        code = error.code
        logger.warning(
            "manual_edit_rejected code=%s reason=%s subject_id=%s occurrence_id=%s",
            code,
            error.reason or "not_applicable",
            subject_id,
            occurrence_id,
        )
        if code in {
            "proposal_patch_invalid",
            "proposal_target_mismatch",
            "proposal_operation_unsupported",
            "synthesis_relation_not_evidenced",
            "synthesis_unknown_evidence",
            "synthesis_unknown_technical_value",
            "synthesis_unknown_date",
            "manual_edit_new_fact_unadmitted",
        }:
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif code == "publication_kind_unresolved":
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif code == "proposal_target_ambiguous":
            response_status = status.HTTP_409_CONFLICT
        elif code in {
            "proposal_patch_unavailable",
            "proposal_patch_hash_mismatch",
            "proposal_base_unavailable",
            "proposal_base_hash_mismatch",
            "proposal_policy_unavailable",
            "proposal_candidate_blob_unavailable",
            "proposal_candidate_blob_hash_mismatch",
            "proposal_acceptance_incomplete",
            "proposal_registry_lineage_unavailable",
        }:
            response_status = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            response_status = status.HTTP_409_CONFLICT
        message = _ERROR_MESSAGES.get(code)
        if code in {
            "proposal_base_stale",
            "proposal_state_conflict",
            "proposal_stale",
            "adoption_placement_changed",
        }:
            code = "manual_edit_stale"
            message = _ERROR_MESSAGES[code]
        elif code == "publication_kind_unresolved":
            code = "manual_edit_format_uneditable"
            message = _ERROR_MESSAGES[code]
        elif code == "proposal_target_mismatch":
            code = "manual_edit_target_mismatch"
            message = _ERROR_MESSAGES[code]
        elif code == "proposal_target_ambiguous":
            code = "manual_edit_target_ambiguous"
            message = _ERROR_MESSAGES[code]
        if code in {
            "synthesis_relation_not_evidenced",
            "synthesis_unknown_evidence",
            "synthesis_unknown_technical_value",
            "synthesis_unknown_date",
        }:
            code = "manual_edit_new_fact_unadmitted"
            message = _ERROR_MESSAGES[code]
        if response_status == status.HTTP_503_SERVICE_UNAVAILABLE:
            code = "manual_edit_unavailable"
            message = _ERROR_MESSAGES[code]
        raise HTTPException(
            status_code=response_status,
            detail={
                "code": code,
                "message": message or "La modification a été refusée. Rechargez l’atelier.",  # noqa: RUF001
            },
        ) from error
    except Exception as error:
        trace = traceback.extract_tb(error.__traceback__)
        origin_frame = trace[-1] if trace else None
        previous_frame = trace[-2] if len(trace) > 1 else None
        origin = (
            f"{origin_frame.filename.rsplit('/', 1)[-1]}:{origin_frame.lineno}:{origin_frame.name}"
            if origin_frame is not None
            else "unavailable"
        )
        previous_origin = (
            f"{previous_frame.filename.rsplit('/', 1)[-1]}:"
            f"{previous_frame.lineno}:{previous_frame.name}"
            if previous_frame is not None
            else "unavailable"
        )
        logger.warning(
            "manual_edit_rejected code=%s reason=%s "
            "exception_class=%s origin=%s previous_origin=%s "
            "subject_id=%s occurrence_id=%s",
            "manual_edit_unexpected",
            "not_applicable",
            type(error).__name__,
            origin,
            previous_origin,
            subject_id,
            occurrence_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "manual_edit_unavailable",
                "message": _ERROR_MESSAGES["manual_edit_unavailable"],
            },
        ) from error

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

"""HTTP API for the append-only edition publication review checkpoint."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cti_app.api.production import ProductionReconciliationView, reconciliation_view
from cti_app.application.edition_publication import (
    EditionPublicationService,
    EditionReleaseStatus,
    PublicationAcceptanceError,
    PublicationManifestNotFoundError,
)
from cti_app.application.edition_release_materialization import (
    EditionReleaseMaterializationError,
)
from cti_app.application.edition_review import (
    EditionReview,
    EditionReviewItemNotFoundError,
    EditionReviewNotFoundError,
    EditionReviewService,
    EditionReviewStatusError,
    InvalidReviewDocumentError,
    InvalidReviewReasonError,
    ReviewItemStaleError,
)
from cti_app.application.identity import IdentityProvider
from cti_app.domain.jobs import JobStatus
from cti_app.domain.production import SubjectProductionStage, SubjectProductionStatus
from cti_app.domain.publication_review import PublicationDecision, PublicationReviewDecision
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api", tags=["publication"])
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ReviewRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_run_id: UUID
    pipeline_generation: Annotated[int, Field(ge=0)]


class ReviewDocumentRequest(ReviewRunRequest):
    document_artifact_id: UUID
    document_artifact_version: Annotated[int, Field(ge=1)]
    document_input_hash: Annotated[str, Field(pattern=_SHA256_PATTERN)]


class IncludeReviewRequest(ReviewDocumentRequest):
    reason: str | None = Field(default=None, max_length=500)


class ExcludeReviewRequest(ReviewRunRequest):
    document_artifact_id: UUID | None = None
    document_artifact_version: Annotated[int | None, Field(ge=1)] = None
    document_input_hash: Annotated[str | None, Field(pattern=_SHA256_PATTERN)] = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def document_identity_is_atomic(self) -> ExcludeReviewRequest:
        values = (
            self.document_artifact_id,
            self.document_artifact_version,
            self.document_input_hash,
        )
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("document identity must be complete or empty")
        return self


class ReviewItemView(BaseModel):
    position: int
    subject_id: UUID
    title: str
    run_id: UUID
    pipeline_generation: int
    run_status: SubjectProductionStatus
    document_artifact_id: UUID | None
    document_artifact_version: int | None
    document_input_hash: str | None
    error_code: str | None
    error_message: str | None
    effective_decision: PublicationDecision | None
    effective_decision_id: UUID | None
    included: bool
    blocking: bool
    rejected_indicator_count: int = 0
    rejected_rule_count: int = 0
    published_rule_count: int = 0
    # The frontend must never infer the retry policy from an error message:
    # ``can_retry`` and ``requires_reconciliation`` are mutually exclusive and
    # each names exactly one operator action.
    can_retry: bool
    retry_stage: SubjectProductionStage | None
    requires_reconciliation: bool = False
    reconciliation: ProductionReconciliationView | None = None


class EditionReviewView(BaseModel):
    edition_id: UUID
    items: list[ReviewItemView]
    can_accept: bool


class ReviewDecisionView(BaseModel):
    id: UUID
    edition_id: UUID
    subject_id: UUID
    production_run_id: UUID
    pipeline_generation: int
    document_artifact_id: UUID | None
    document_artifact_version: int | None
    document_input_hash: str | None
    decision: PublicationDecision
    actor_id: str
    reason: str | None
    occurred_at: str


class PublicationAcceptView(BaseModel):
    edition_id: UUID
    edition_status: str
    manifest_id: UUID
    manifest_sha256: str
    edition_version: int
    batch_id: UUID
    job_id: UUID | None
    job_dispatched: bool


class EditionReleaseView(BaseModel):
    edition_id: UUID
    edition_status: str
    manifest_id: UUID | None
    manifest_sha256: str | None
    release_id: UUID | None
    json_available: bool
    markdown_available: bool
    docx_available: bool
    published_at: str | None
    assembly_job_id: UUID | None
    assembly_status: JobStatus | None
    assembly_error_code: str | None
    assembly_error_message: str | None
    can_retry_assembly: bool


class EditionReleaseMaterializationView(BaseModel):
    edition_id: UUID
    materialized: bool


def _service(request: Request) -> EditionReviewService:
    configured = getattr(request.app.state, "edition_review_service", None)
    return configured or EditionReviewService(request.app.state.uow_factory)


def _publication_service(request: Request) -> EditionPublicationService:
    configured = getattr(request.app.state, "edition_publication_service", None)
    if configured is None:
        configured = EditionPublicationService(
            request.app.state.uow_factory,
            request.app.state.production_artifact_store,
            job_service=getattr(request.app.state, "job_service", None),
            job_dispatcher=getattr(request.app.state, "job_dispatcher", None),
        )
    return configured


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


@router.get("/editions/{edition_id}/review", response_model=EditionReviewView)
async def get_edition_review(edition_id: UUID, request: Request) -> EditionReviewView:
    try:
        return _review_view(await _service(request).get(edition_id))
    except Exception as exc:
        _raise_review_error(exc)


@router.post(
    "/editions/{edition_id}/review/items/{subject_id}/include",
    response_model=ReviewDecisionView,
)
async def include_review_item(
    edition_id: UUID,
    subject_id: UUID,
    payload: IncludeReviewRequest,
    request: Request,
) -> ReviewDecisionView:
    return await _decide(
        edition_id,
        subject_id,
        payload,
        request,
        PublicationDecision.INCLUDE,
    )


@router.post(
    "/editions/{edition_id}/review/items/{subject_id}/exclude",
    response_model=ReviewDecisionView,
)
async def exclude_review_item(
    edition_id: UUID,
    subject_id: UUID,
    payload: ExcludeReviewRequest,
    request: Request,
) -> ReviewDecisionView:
    return await _decide(
        edition_id,
        subject_id,
        payload,
        request,
        PublicationDecision.EXCLUDE,
    )


@router.post(
    "/editions/{edition_id}/publication/accept",
    response_model=PublicationAcceptView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def accept_edition_publication(edition_id: UUID, request: Request) -> PublicationAcceptView:
    try:
        result = await _publication_service(request).accept(
            edition_id,
            actor_id=await _actor_id(request),
            correlation_id=get_correlation_id(),
        )
        return PublicationAcceptView(
            edition_id=result.manifest.edition_id,
            edition_status=result.edition_status.value,
            manifest_id=result.manifest.id,
            manifest_sha256=result.manifest.content_sha256,
            edition_version=result.manifest.edition_version,
            batch_id=result.manifest.batch_id,
            job_id=result.job_id,
            job_dispatched=result.job_dispatched,
        )
    except Exception as exc:
        _raise_publication_error(exc)


@router.get("/editions/{edition_id}/release", response_model=EditionReleaseView)
async def get_edition_release(edition_id: UUID, request: Request) -> EditionReleaseView:
    try:
        release = await _publication_service(request).release_status(edition_id)
        return _release_view(release)
    except Exception as exc:
        _raise_publication_error(exc)


@router.post(
    "/editions/{edition_id}/release/materialize",
    response_model=EditionReleaseMaterializationView,
)
async def materialize_edition_release(
    edition_id: UUID, request: Request
) -> EditionReleaseMaterializationView:
    """Rebuild only the disposable release projection from canonical storage."""
    materializer = getattr(request.app.state, "edition_release_rematerializer", None)
    if materializer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "release_materialization_unavailable"},
        )
    await _actor_id(request)
    try:
        await materializer.materialize(edition_id)
    except EditionReleaseMaterializationError as exc:
        code = str(exc)
        status_code = (
            status.HTTP_404_NOT_FOUND
            if code in {"edition_not_found", "manifest_not_found", "edition_release_not_found"}
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=status_code, detail={"code": code}) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "release_workspace_unavailable"},
        ) from exc
    return EditionReleaseMaterializationView(edition_id=edition_id, materialized=True)


@router.get("/editions/{edition_id}/release/docx")
async def download_edition_docx(edition_id: UUID, request: Request) -> Response:
    try:
        _, content = await _publication_service(request).read_docx(edition_id)
    except Exception as exc:
        _raise_publication_error(exc)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="bulletin-{edition_id}.docx"'},
    )


async def _decide(
    edition_id: UUID,
    subject_id: UUID,
    payload: IncludeReviewRequest | ExcludeReviewRequest,
    request: Request,
    decision: PublicationDecision,
) -> ReviewDecisionView:
    try:
        reason = getattr(payload, "reason", None)
        event = await _service(request).decide(
            edition_id,
            subject_id,
            decision=decision,
            production_run_id=payload.production_run_id,
            pipeline_generation=payload.pipeline_generation,
            document_artifact_id=payload.document_artifact_id,
            document_artifact_version=payload.document_artifact_version,
            document_input_hash=payload.document_input_hash,
            actor_id=await _actor_id(request),
            reason=reason,
        )
        return _decision_view(event)
    except Exception as exc:
        _raise_review_error(exc)


def _review_view(review: EditionReview) -> EditionReviewView:
    return EditionReviewView(
        edition_id=review.edition_id,
        items=[
            ReviewItemView(
                position=item.position,
                subject_id=item.subject_id,
                title=item.title,
                run_id=item.run_id,
                pipeline_generation=item.pipeline_generation,
                run_status=item.run_status.value,
                document_artifact_id=item.document_artifact_id,
                document_artifact_version=item.document_artifact_version,
                document_input_hash=item.document_input_hash,
                error_code=item.error_code,
                error_message=item.error_message,
                effective_decision=item.effective_decision,
                effective_decision_id=item.effective_decision_id,
                included=item.included,
                blocking=item.blocking,
                rejected_indicator_count=item.rejected_indicator_count,
                rejected_rule_count=item.rejected_rule_count,
                published_rule_count=item.published_rule_count,
                can_retry=item.can_retry,
                retry_stage=item.retry_stage,
                requires_reconciliation=item.requires_reconciliation,
                reconciliation=reconciliation_view(
                    item.run_id,
                    item.reconciliation,
                    pipeline_generation=item.pipeline_generation,
                ),
            )
            for item in review.items
        ],
        can_accept=review.can_accept,
    )


def _decision_view(decision: PublicationReviewDecision) -> ReviewDecisionView:
    return ReviewDecisionView(
        id=decision.id,
        edition_id=decision.edition_id,
        subject_id=decision.subject_id,
        production_run_id=decision.production_run_id,
        pipeline_generation=decision.pipeline_generation,
        document_artifact_id=decision.document_artifact_id,
        document_artifact_version=decision.document_artifact_version,
        document_input_hash=decision.document_input_hash,
        decision=decision.decision,
        actor_id=decision.actor_id,
        reason=decision.reason,
        occurred_at=decision.occurred_at.isoformat(),
    )


def _release_view(release: EditionReleaseStatus) -> EditionReleaseView:
    return EditionReleaseView(
        edition_id=release.edition_id,
        edition_status=release.edition_status.value,
        manifest_id=release.manifest_id,
        manifest_sha256=release.manifest_sha256,
        release_id=release.release.id if release.release is not None else None,
        json_available=release.json_available,
        markdown_available=release.markdown_available,
        docx_available=release.docx_available,
        published_at=release.published_at.isoformat() if release.published_at else None,
        assembly_job_id=release.assembly_job_id,
        assembly_status=release.assembly_status,
        assembly_error_code=release.assembly_error_code,
        assembly_error_message=release.assembly_error_message,
        can_retry_assembly=release.can_retry_assembly,
    )


def _raise_review_error(exc: Exception) -> NoReturn:
    if isinstance(exc, EditionReviewNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Edition not found"
        ) from exc
    if isinstance(exc, EditionReviewItemNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Review item not found"
        ) from exc
    if isinstance(exc, ReviewItemStaleError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "review_item_stale"},
        ) from exc
    if isinstance(exc, EditionReviewStatusError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "edition_must_be_in_review"},
        ) from exc
    if isinstance(exc, InvalidReviewReasonError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_review_reason"},
        ) from exc
    if isinstance(exc, InvalidReviewDocumentError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_review_document"},
        ) from exc
    raise exc


def _raise_publication_error(exc: Exception) -> NoReturn:
    if isinstance(exc, PublicationManifestNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Edition release not available"
        ) from exc
    if isinstance(exc, PublicationAcceptanceError):
        code = str(exc)
        if code == "edition_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Edition not found"
            ) from exc
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": code}) from exc
    raise exc

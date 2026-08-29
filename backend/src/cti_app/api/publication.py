"""HTTP API for the append-only edition publication review checkpoint."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.edition_review import (
    EditionReview,
    EditionReviewItemNotFoundError,
    EditionReviewNotFoundError,
    EditionReviewService,
    EditionReviewStatusError,
    InvalidReviewReasonError,
    ReviewItemStaleError,
)
from cti_app.application.identity import IdentityProvider
from cti_app.domain.publication_review import PublicationDecision, PublicationReviewDecision

router = APIRouter(prefix="/api", tags=["publication"])
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ReviewDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_run_id: UUID
    pipeline_generation: Annotated[int, Field(ge=0)]
    document_artifact_id: UUID
    document_artifact_version: Annotated[int, Field(ge=1)]
    document_input_hash: Annotated[str, Field(pattern=_SHA256_PATTERN)]


class IncludeReviewRequest(ReviewDocumentRequest):
    reason: str | None = Field(default=None, max_length=500)


class ExcludeReviewRequest(ReviewDocumentRequest):
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ReviewItemView(BaseModel):
    position: int
    subject_id: UUID
    title: str
    run_id: UUID
    pipeline_generation: int
    run_status: str
    document_artifact_id: UUID | None
    document_artifact_version: int | None
    document_input_hash: str | None
    error_code: str | None
    error_message: str | None
    effective_decision: PublicationDecision | None
    included: bool
    blocking: bool
    can_retry: bool


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
    document_artifact_id: UUID
    document_artifact_version: int
    document_input_hash: str
    decision: PublicationDecision
    actor_id: str
    reason: str | None
    occurred_at: str


def _service(request: Request) -> EditionReviewService:
    configured = getattr(request.app.state, "edition_review_service", None)
    return configured or EditionReviewService(request.app.state.uow_factory)


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


async def _decide(
    edition_id: UUID,
    subject_id: UUID,
    payload: ReviewDocumentRequest,
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
                included=item.included,
                blocking=item.blocking,
                can_retry=item.can_retry,
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
    raise exc

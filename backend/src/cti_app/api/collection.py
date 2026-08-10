from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.collection import (
    CollectionItemNotFoundError,
    CollectionNotAllowedError,
    SubjectCollectionService,
    collection_idempotency_key,
)
from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import DuplicateJobError, JobDispatcher, JobService
from cti_app.domain.collection import (
    Claim,
    CollectionAttempt,
    Indicator,
    ReviewStatus,
    SourceCollection,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editorial import HumanDecision, HumanDecisionType
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api/subjects", tags=["subject-workbench"])


class CollectionLaunchView(BaseModel):
    job_id: UUID
    duplicate: bool


class AttemptView(BaseModel):
    id: UUID
    requested_url: str
    final_url: str | None
    redirect_chain: list[str]
    attempted_at: str
    completed_at: str
    http_status: int | None
    declared_content_type: str | None
    detected_content_type: str | None
    encoded_size: int | None
    encoded_sha256: str | None
    decoded_size: int | None
    decoded_sha256: str | None
    content_encoding: str | None
    outcome: str
    failure_reason: str | None


class SourceView(BaseModel):
    id: UUID
    requested_url: str
    state: str
    proposed_role: str
    relationship_status: str
    relationship_evidence: str
    source_document_id: UUID | None
    attempt_count: int
    error_reason: str | None
    fetch_lease_expires_at: str | None
    latest_attempt: AttemptView | None


class ClaimView(BaseModel):
    id: UUID
    kind: str
    original_value: str
    current_value: str
    status: ReviewStatus
    source_id: UUID
    source_span: dict[str, int]
    passage: str
    extraction_payload: dict[str, object]


class IndicatorView(BaseModel):
    id: UUID
    kind: str
    original_value: str
    normalized_value: str
    current_value: str
    status: ReviewStatus
    source_id: UUID
    source_span: dict[str, int]


class WorkbenchView(BaseModel):
    subject_id: UUID
    sources: list[SourceView]
    claims: list[ClaimView]
    indicators: list[IndicatorView]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["validate", "correct", "reject"]
    corrected_value: str | None = Field(default=None, max_length=4000)


class RelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: SourceRole


@router.post(
    "/{subject_id}/collection",
    response_model=CollectionLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def launch_collection(subject_id: UUID, request: Request) -> CollectionLaunchView:
    collection, jobs, dispatcher = _runtime(request)
    actor_id = await _actor_id(request)
    try:
        sources = await collection.initialize(subject_id)
    except CollectionNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    round_number = max(
        (
            item.attempt_count
            + (0 if item.state.value in {"completed", "blocked", "failed_terminal"} else 1)
            for item in sources
        ),
        default=1,
    )
    key = collection_idempotency_key(subject_id, collection.configuration_id, round_number)
    duplicate = False
    try:
        job = await jobs.submit(
            kind="source.collect",
            aggregate_type="subject",
            aggregate_id=subject_id,
            idempotency_key=key,
            correlation_id=get_correlation_id(),
            input_parameters={"subject_id": str(subject_id), "requested_by": actor_id},
            max_attempts=3,
            actor_id=actor_id,
        )
        await dispatcher.dispatch(job.id)
    except DuplicateJobError as exc:
        job = await jobs.get(exc.existing_job_id)
        duplicate = True
    return CollectionLaunchView(job_id=job.id, duplicate=duplicate)


@router.post(
    "/{subject_id}/sources/{collection_id}/retry",
    response_model=CollectionLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_source(
    subject_id: UUID, collection_id: UUID, request: Request
) -> CollectionLaunchView:
    collection, jobs, dispatcher = _runtime(request)
    sources = await collection.list_sources(subject_id)
    source = next((item for item in sources if item.id == collection_id), None)
    if source is None:
        raise HTTPException(status_code=404, detail="Source collection not found")
    try:
        source = await collection.prepare_retry(collection_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    actor_id = await _actor_id(request)
    key = collection_idempotency_key(
        subject_id,
        collection.configuration_id,
        source.attempt_count + 1,
        collection_id=source.id,
    )
    duplicate = False
    try:
        job = await jobs.submit(
            kind="source.collect",
            aggregate_type="source_collection",
            aggregate_id=source.id,
            idempotency_key=key,
            correlation_id=get_correlation_id(),
            input_parameters={
                "subject_id": str(subject_id),
                "collection_id": str(source.id),
                "requested_by": actor_id,
            },
            max_attempts=3,
            actor_id=actor_id,
        )
        await dispatcher.dispatch(job.id)
    except DuplicateJobError as exc:
        job = await jobs.get(exc.existing_job_id)
        duplicate = True
    return CollectionLaunchView(job_id=job.id, duplicate=duplicate)


@router.get("/{subject_id}/workbench", response_model=WorkbenchView)
async def get_workbench(subject_id: UUID, request: Request) -> WorkbenchView:
    service, _, _ = _runtime(request)
    if not await service.subject_exists(subject_id):
        raise HTTPException(status_code=404, detail="Subject not found")
    sources = await service.list_sources(subject_id)
    claims, indicators = await service.list_evidence(subject_id)
    if sources:
        decisions = await service.decisions(sources[0].edition_id)
    else:
        decisions = []
    source_views: list[SourceView] = []
    for source in sources:
        attempts = await service.attempts(source.id)
        source_views.append(_source_view(source, attempts[-1] if attempts else None))
    claim_views: list[ClaimView] = []
    text_cache: dict[UUID, str] = {}
    for claim in claims:
        text = text_cache.get(claim.derived_artifact_id)
        if text is None:
            text = await service.extracted_text(claim.derived_artifact_id)
            text_cache[claim.derived_artifact_id] = text
        claim_views.append(_claim_view(claim, text, decisions))
    return WorkbenchView(
        subject_id=subject_id,
        sources=source_views,
        claims=claim_views,
        indicators=[_indicator_view(item, decisions) for item in indicators],
    )


@router.post("/{subject_id}/claims/{claim_id}/decision", response_model=ClaimView)
async def decide_claim(
    subject_id: UUID, claim_id: UUID, payload: ReviewRequest, request: Request
) -> ClaimView:
    service, _, _ = _runtime(request)
    decision_type = HumanDecisionType(f"claim_{payload.action}")
    try:
        claim = await service.get_claim(claim_id)
        if claim.subject_id != subject_id:
            raise CollectionItemNotFoundError(str(claim_id))
        await service.decide_claim(
            claim_id,
            decision_type,
            actor_id=await _actor_id(request),
            correlation_id=get_correlation_id(),
            corrected_value=payload.corrected_value,
        )
    except CollectionItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Claim not found") from exc
    decisions = await service.decisions(claim.edition_id)
    text = await service.extracted_text(claim.derived_artifact_id)
    return _claim_view(claim, text, decisions)


@router.post("/{subject_id}/indicators/{indicator_id}/decision", response_model=IndicatorView)
async def decide_indicator(
    subject_id: UUID, indicator_id: UUID, payload: ReviewRequest, request: Request
) -> IndicatorView:
    service, _, _ = _runtime(request)
    claims, indicators = await service.list_evidence(subject_id)
    del claims
    indicator = next((item for item in indicators if item.id == indicator_id), None)
    if indicator is None:
        raise HTTPException(status_code=404, detail="Indicator not found")
    await service.decide_indicator(
        indicator_id,
        HumanDecisionType(f"indicator_{payload.action}"),
        actor_id=await _actor_id(request),
        correlation_id=get_correlation_id(),
        corrected_value=payload.corrected_value,
    )
    return _indicator_view(indicator, await service.decisions(indicator.edition_id))


@router.post("/{subject_id}/sources/{collection_id}/relationship", response_model=SourceView)
async def decide_relationship(
    subject_id: UUID,
    collection_id: UUID,
    payload: RelationshipRequest,
    request: Request,
) -> SourceView:
    service, _, _ = _runtime(request)
    source_for_subject = next(
        (item for item in await service.list_sources(subject_id) if item.id == collection_id),
        None,
    )
    if source_for_subject is None:
        raise HTTPException(status_code=404, detail="Source collection not found")
    try:
        source = await service.decide_relationship(
            collection_id,
            payload.role,
            actor_id=await _actor_id(request),
            correlation_id=get_correlation_id(),
        )
    except CollectionItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source collection not found") from exc
    attempts = await service.attempts(source.id)
    return _source_view(source, attempts[-1] if attempts else None)


def _runtime(request: Request) -> tuple[SubjectCollectionService, JobService, JobDispatcher]:
    return (
        request.app.state.collection_service,
        request.app.state.job_service,
        request.app.state.job_dispatcher,
    )


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


def _source_view(source: SourceCollection, attempt: CollectionAttempt | None) -> SourceView:
    return SourceView(
        id=source.id,
        requested_url=source.requested_url,
        state=source.state.value,
        proposed_role=source.proposed_role.value,
        relationship_status=source.relationship_status.value,
        relationship_evidence=source.relationship_evidence,
        source_document_id=source.source_document_id,
        attempt_count=source.attempt_count,
        error_reason=source.error_reason,
        fetch_lease_expires_at=(
            source.fetch_lease_expires_at.isoformat() if source.fetch_lease_expires_at else None
        ),
        latest_attempt=_attempt_view(attempt) if attempt else None,
    )


def _attempt_view(attempt: CollectionAttempt) -> AttemptView:
    return AttemptView(
        id=attempt.id,
        requested_url=attempt.requested_url,
        final_url=attempt.final_url,
        redirect_chain=list(attempt.redirect_chain),
        attempted_at=attempt.attempted_at.isoformat(),
        completed_at=attempt.completed_at.isoformat(),
        http_status=attempt.http_status,
        declared_content_type=attempt.declared_content_type,
        detected_content_type=attempt.detected_content_type,
        encoded_size=attempt.encoded_size,
        encoded_sha256=attempt.encoded_sha256,
        decoded_size=attempt.decoded_size,
        decoded_sha256=attempt.decoded_sha256,
        content_encoding=attempt.content_encoding,
        outcome=attempt.outcome.value,
        failure_reason=attempt.failure_reason,
    )


def _claim_view(claim: Claim, text: str, decisions: list[HumanDecision]) -> ClaimView:
    status_value, current = _review_projection(
        decisions, "claim_id", claim.id, claim.value, "claim_"
    )
    return ClaimView(
        id=claim.id,
        kind=claim.kind.value,
        original_value=claim.value,
        current_value=current,
        status=status_value,
        source_id=claim.source_document_id,
        source_span={"start": claim.span.start, "end": claim.span.end},
        passage=claim.span.passage(text),
        extraction_payload=claim.extraction_payload,
    )


def _indicator_view(indicator: Indicator, decisions: list[HumanDecision]) -> IndicatorView:
    status_value, current = _review_projection(
        decisions,
        "indicator_id",
        indicator.id,
        indicator.normalized_value,
        "indicator_",
    )
    return IndicatorView(
        id=indicator.id,
        kind=indicator.kind.value,
        original_value=indicator.original_value,
        normalized_value=indicator.normalized_value,
        current_value=current,
        status=status_value,
        source_id=indicator.source_document_id,
        source_span={"start": indicator.span.start, "end": indicator.span.end},
    )


def _review_projection(
    decisions: list[HumanDecision], key: str, target_id: UUID, original: str, prefix: str
) -> tuple[ReviewStatus, str]:
    relevant = [item for item in decisions if item.payload.get(key) == str(target_id)]
    if not relevant:
        return ReviewStatus.EXTRACTED, original
    latest = relevant[-1]
    action = latest.decision_type.value.removeprefix(prefix)
    status_value = ReviewStatus(action + "d" if action == "validate" else action + "ed")
    current = latest.payload.get("corrected_value")
    return status_value, str(current) if current else original

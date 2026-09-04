from __future__ import annotations

from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile

from cti_app.application.collection import (
    ManualContentAlreadyArchivedError,
    ManualContentEmptyError,
    ManualContentTooLargeError,
    ManualContentTypeError,
    SubjectCollectionService,
    collection_idempotency_key,
)
from cti_app.application.collection_errors import (
    CollectionItemNotFoundError,
    CollectionNotAllowedError,
)
from cti_app.application.collection_review import CollectionReviewService
from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import DuplicateJobError, JobDispatcher, JobService
from cti_app.application.production_verification import project_review_status
from cti_app.application.source_filenames import ascii_download_filename, validate_logical_filename
from cti_app.domain.collection import (
    Claim,
    CollectionAttempt,
    CollectionState,
    Indicator,
    ReviewStatus,
    SourceCollection,
)
from cti_app.domain.discovery import SourceCandidate, SourceRole
from cti_app.domain.editorial import HumanDecision, HumanDecisionType
from cti_app.domain.entities import SourceDocument
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
    title: str
    publisher: str
    published_at: str | None
    tlp: str | None
    logical_filename: str | None
    detected_mime_type: str | None


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


class ManualContentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    declared_mime_type: str = Field(min_length=1, max_length=255)
    final_url: str | None = None


@router.post(
    "/{subject_id}/collection",
    response_model=CollectionLaunchView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def launch_collection(subject_id: UUID, request: Request) -> CollectionLaunchView:
    collection, _review, jobs, dispatcher = _runtime(request)
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
    key = collection_idempotency_key(subject_id, collection.policy_snapshot.id, round_number)
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
    collection, _review, jobs, dispatcher = _runtime(request)
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
        collection.policy_snapshot.id,
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


@router.post(
    "/{subject_id}/sources/{collection_id}/content",
    response_model=SourceView,
)
async def archive_manual_source_content(
    subject_id: UUID, collection_id: UUID, request: Request
) -> SourceView:
    service, _review, _, _ = _runtime(request)
    source_for_subject = next(
        (item for item in await service.list_sources(subject_id) if item.id == collection_id),
        None,
    )
    if source_for_subject is None:
        raise HTTPException(status_code=404, detail="Source collection not found")
    if source_for_subject.state in {
        CollectionState.ARCHIVED,
        CollectionState.EXTRACTED,
        CollectionState.COMPLETED,
    }:
        raise HTTPException(status_code=409, detail="source_already_archived")

    content, declared_mime_type, final_url = await _manual_content_payload(request)
    try:
        source = await service.archive_manual_content(
            collection_id,
            content=content,
            declared_mime_type=declared_mime_type,
            final_url=final_url,
            actor_id=await _actor_id(request),
        )
    except CollectionItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source collection not found") from exc
    except ManualContentAlreadyArchivedError as exc:
        raise HTTPException(status_code=409, detail="source_already_archived") from exc
    except ManualContentTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except (ManualContentEmptyError, ManualContentTooLargeError) as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except CollectionNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    attempts = await service.attempts(source.id)
    candidate, document = await service.source_context(source)
    return _source_view(source, attempts[-1] if attempts else None, candidate, document)


@router.get("/{subject_id}/workbench", response_model=WorkbenchView)
async def get_workbench(subject_id: UUID, request: Request) -> WorkbenchView:
    service, review, _, _ = _runtime(request)
    if not await service.subject_exists(subject_id):
        raise HTTPException(status_code=404, detail="Subject not found")
    sources = await service.list_sources(subject_id)
    claims, indicators = await review.list_evidence(subject_id)
    if sources:
        decisions = await review.decisions(sources[0].edition_id)
    else:
        decisions = []
    source_views: list[SourceView] = []
    for source in sources:
        attempts = await service.attempts(source.id)
        candidate, document = await service.source_context(source)
        source_views.append(
            _source_view(source, attempts[-1] if attempts else None, candidate, document)
        )
    claim_views: list[ClaimView] = []
    text_cache: dict[UUID, str] = {}
    for claim in claims:
        text = text_cache.get(claim.derived_artifact_id)
        if text is None:
            text = await review.extracted_text(claim.derived_artifact_id)
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
    _service, review, _, _ = _runtime(request)
    decision_type = HumanDecisionType(f"claim_{payload.action}")
    try:
        claim = await review.get_claim(claim_id)
        if claim.subject_id != subject_id:
            raise CollectionItemNotFoundError(str(claim_id))
        await review.decide_claim(
            claim_id,
            decision_type,
            actor_id=await _actor_id(request),
            correlation_id=get_correlation_id(),
            corrected_value=payload.corrected_value,
        )
    except CollectionItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Claim not found") from exc
    decisions = await review.decisions(claim.edition_id)
    text = await review.extracted_text(claim.derived_artifact_id)
    return _claim_view(claim, text, decisions)


@router.post("/{subject_id}/indicators/{indicator_id}/decision", response_model=IndicatorView)
async def decide_indicator(
    subject_id: UUID, indicator_id: UUID, payload: ReviewRequest, request: Request
) -> IndicatorView:
    _service, review, _, _ = _runtime(request)
    claims, indicators = await review.list_evidence(subject_id)
    del claims
    indicator = next((item for item in indicators if item.id == indicator_id), None)
    if indicator is None:
        raise HTTPException(status_code=404, detail="Indicator not found")
    await review.decide_indicator(
        indicator_id,
        HumanDecisionType(f"indicator_{payload.action}"),
        actor_id=await _actor_id(request),
        correlation_id=get_correlation_id(),
        corrected_value=payload.corrected_value,
    )
    return _indicator_view(indicator, await review.decisions(indicator.edition_id))


@router.post("/{subject_id}/sources/{collection_id}/relationship", response_model=SourceView)
async def decide_relationship(
    subject_id: UUID,
    collection_id: UUID,
    payload: RelationshipRequest,
    request: Request,
) -> SourceView:
    service, review, _, _ = _runtime(request)
    source_for_subject = next(
        (item for item in await service.list_sources(subject_id) if item.id == collection_id),
        None,
    )
    if source_for_subject is None:
        raise HTTPException(status_code=404, detail="Source collection not found")
    try:
        source = await review.decide_relationship(
            collection_id,
            payload.role,
            actor_id=await _actor_id(request),
            correlation_id=get_correlation_id(),
        )
    except CollectionItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source collection not found") from exc
    attempts = await service.attempts(source.id)
    candidate, document = await service.source_context(source)
    return _source_view(source, attempts[-1] if attempts else None, candidate, document)


@router.get("/{subject_id}/sources/{collection_id}/download")
async def download_source(subject_id: UUID, collection_id: UUID, request: Request) -> Response:
    service, _review, _, _ = _runtime(request)
    try:
        document, content = await service.download_source(subject_id, collection_id)
    except CollectionItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source collection not found") from exc
    except CollectionNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logical_filename = validate_logical_filename(
        document.logical_filename or document.original_name
    )
    ascii_filename = ascii_download_filename(logical_filename).replace('"', "_")
    disposition = (
        f'attachment; filename="{ascii_filename}"; '
        f"filename*=UTF-8''{quote(logical_filename, safe='')}"
    )
    return Response(
        content,
        media_type=document.detected_mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _runtime(
    request: Request,
) -> tuple[
    SubjectCollectionService,
    CollectionReviewService,
    JobService,
    JobDispatcher,
]:
    return (
        request.app.state.collection_service,
        request.app.state.collection_review_service,
        request.app.state.job_service,
        request.app.state.job_dispatcher,
    )


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


async def _manual_content_payload(request: Request) -> tuple[bytes, str, str | None]:
    content_type = request.headers.get("content-type", "").casefold()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        uploaded = form.get("file")
        if isinstance(uploaded, UploadFile):
            content = await uploaded.read()
            declared = form.get("declared_mime_type") or uploaded.content_type
            declared_mime_type = str(declared or "application/octet-stream")
        else:
            raw_content = form.get("content", "")
            content = str(raw_content).encode("utf-8")
            declared_mime_type = str(form.get("declared_mime_type") or "text/html")
        raw_final_url = form.get("final_url")
        final_url = str(raw_final_url) if raw_final_url else None
        return content, declared_mime_type, final_url

    try:
        payload = ManualContentRequest.model_validate(await request.json())
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=422, detail="Invalid manual source content payload"
        ) from exc
    return payload.content.encode("utf-8"), payload.declared_mime_type, payload.final_url


def _source_view(
    source: SourceCollection,
    attempt: CollectionAttempt | None,
    candidate: SourceCandidate | None,
    document: SourceDocument | None,
) -> SourceView:
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
        title=(
            (candidate.title if candidate else None)
            or (document.title if document else None)
            or "titre-inconnu"
        ),
        publisher=(candidate.publisher if candidate else None)
        or (document.publisher if document else None)
        or "publisher-inconnu",
        published_at=(
            candidate.published_at.isoformat()
            if candidate and candidate.published_at
            else document.published_at.isoformat()
            if document and document.published_at
            else None
        ),
        tlp=(candidate.tlp.value if candidate else document.tlp.value if document else None),
        logical_filename=document.logical_filename if document else None,
        detected_mime_type=(
            document.detected_mime_type
            if document
            else attempt.detected_content_type
            if attempt
            else None
        ),
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
    decisions: list[HumanDecision],
    key: str,
    target_id: UUID,
    original: str,
    prefix: str,
    *,
    machine_verified: bool = False,
) -> tuple[ReviewStatus, str]:
    relevant = [item for item in decisions if item.payload.get(key) == str(target_id)]
    if not relevant:
        # No human looked at it: machine verification is what we can honestly say.
        return project_review_status(None, machine_verified=machine_verified), original
    latest = relevant[-1]
    action = latest.decision_type.value.removeprefix(prefix)
    status_value = ReviewStatus(action + "d" if action == "validate" else action + "ed")
    current = latest.payload.get("corrected_value")
    return status_value, str(current) if current else original

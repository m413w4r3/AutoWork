from __future__ import annotations

from datetime import date, datetime
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.discovery.fusion import (
    FusionBoard,
    FusionCandidate,
    FusionDeterministicSignal,
    FusionEditionArchivedError,
    FusionModelSuggestion,
    FusionReviewDecision,
    FusionSelectedSubjectConflictError,
    FusionService,
    FusionSnapshotStaleError,
)
from cti_app.application.identity import IdentityProvider
from cti_app.domain.discovery_cumulative import FusionReviewAction

fusion_router = APIRouter(prefix="/api/editions/{edition_id}/fusion", tags=["fusion"])


class FusionPublicationView(BaseModel):
    id: UUID
    url: str
    canonical_url: str
    title: str
    publisher: str
    published_at: date | None


class FusionCandidateView(BaseModel):
    id: UUID
    discovery_run_id: UUID
    discovery_batch_id: UUID
    supersedes_candidate_id: UUID | None
    title: str
    summary: str
    event_date: date | None
    actors: list[str]
    campaigns: list[str]
    malware: list[str]
    cves: list[str]
    iocs: list[str]
    countries: list[str]
    sectors: list[str]
    likely_artifacts: list[str]
    publications: list[FusionPublicationView]


class FusionSignalView(BaseModel):
    kind: str
    value: str
    candidate_ids: list[UUID]


class FusionSuggestionView(BaseModel):
    recommendation: str
    summary: str


class FusionHistoryView(BaseModel):
    action: str
    merge_run_id: UUID | None
    planner_kind: str | None
    actor_id: str | None
    candidate_ids: list[UUID]
    created_at: datetime


class FusionGroupView(BaseModel):
    discovery_subject_id: UUID
    title: str
    summary: str
    candidate_ids: list[UUID]
    candidates: list[FusionCandidateView]
    confidence: str | None
    origin: str | None
    resolution_state: str
    deterministic_signals: list[FusionSignalView]
    model_suggestion: FusionSuggestionView | None
    differences: list[str]
    history: list[FusionHistoryView]


class FusionReviewGroupView(BaseModel):
    candidate_ids: list[UUID]
    candidates: list[FusionCandidateView]
    proposed_discovery_subject_ids: list[UUID]
    confidence: str
    requires_decision: bool
    deterministic_signals: list[FusionSignalView]
    model_suggestion: FusionSuggestionView | None
    differences: list[str]


class FusionPendingReviewView(BaseModel):
    merge_run_id: UUID
    candidate_ids: list[UUID]
    discovery_subject_ids: list[UUID]
    review_reasons: list[str]
    stale: bool
    groups: list[FusionReviewGroupView]
    created_at: datetime


class FusionBoardView(BaseModel):
    edition_id: UUID
    snapshot_id: UUID | None
    snapshot_version: int | None
    read_only: bool
    candidate_count: int
    group_count: int
    pending_review_count: int
    groups: list[FusionGroupView]
    pending_reviews: list[FusionPendingReviewView]
    unstabilized_candidates: list[FusionCandidateView]


class FusionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: FusionReviewAction
    candidate_ids: list[UUID] = Field(min_length=1)
    target_discovery_subject_id: UUID | None = None


class FusionReviewResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 0 designates "no snapshot yet": a review parked on the very first intake.
    snapshot_version: int = Field(ge=0)
    decisions: list[FusionDecisionRequest] = Field(min_length=1)


class FusionMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_version: int = Field(ge=1)
    discovery_subject_ids: list[UUID] = Field(min_length=2, max_length=100)


class FusionSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_version: int = Field(ge=1)
    discovery_subject_id: UUID
    candidate_ids: list[UUID] = Field(min_length=1, max_length=100)


@fusion_router.get("", response_model=FusionBoardView)
async def read_fusion_board(edition_id: UUID, request: Request) -> FusionBoardView:
    service: FusionService = request.app.state.fusion_service
    try:
        return _board_view(await service.get_board(edition_id))
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "edition_not_found", "message": str(exc)}
        ) from exc


@fusion_router.post("/reviews/{merge_run_id}/resolve", response_model=FusionBoardView)
async def resolve_fusion_review(
    edition_id: UUID,
    merge_run_id: UUID,
    payload: FusionReviewResolveRequest,
    request: Request,
) -> FusionBoardView:
    service: FusionService = request.app.state.fusion_service
    identity: IdentityProvider = request.app.state.identity_provider
    actor = await identity.current()
    try:
        board = await service.resolve_review(
            edition_id,
            merge_run_id,
            snapshot_version=payload.snapshot_version,
            decisions=[
                FusionReviewDecision(
                    action=item.action,
                    candidate_ids=tuple(item.candidate_ids),
                    target_discovery_subject_id=item.target_discovery_subject_id,
                )
                for item in payload.decisions
            ],
            actor_id=actor.actor_id,
        )
    except Exception as exc:
        _raise_fusion_error(exc)
    return _board_view(board)


@fusion_router.post("/merge", response_model=FusionBoardView)
async def merge_fusion_subjects(
    edition_id: UUID, payload: FusionMergeRequest, request: Request
) -> FusionBoardView:
    service: FusionService = request.app.state.fusion_service
    identity: IdentityProvider = request.app.state.identity_provider
    actor = await identity.current()
    try:
        board = await service.merge(
            edition_id,
            snapshot_version=payload.snapshot_version,
            discovery_subject_ids=payload.discovery_subject_ids,
            actor_id=actor.actor_id,
        )
    except Exception as exc:
        _raise_fusion_error(exc)
    return _board_view(board)


@fusion_router.post("/split", response_model=FusionBoardView)
async def split_fusion_subject(
    edition_id: UUID, payload: FusionSplitRequest, request: Request
) -> FusionBoardView:
    service: FusionService = request.app.state.fusion_service
    identity: IdentityProvider = request.app.state.identity_provider
    actor = await identity.current()
    try:
        board = await service.split(
            edition_id,
            snapshot_version=payload.snapshot_version,
            discovery_subject_id=payload.discovery_subject_id,
            candidate_ids=payload.candidate_ids,
            actor_id=actor.actor_id,
        )
    except Exception as exc:
        _raise_fusion_error(exc)
    return _board_view(board)


def _raise_fusion_error(exc: Exception) -> NoReturn:
    if isinstance(exc, FusionSnapshotStaleError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": "L'état de la fusion a changé : rechargez-le avant de décider.",
            },
        ) from exc
    if isinstance(exc, FusionEditionArchivedError):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": "Une édition archivée n'est pas modifiable."},
        ) from exc
    if isinstance(exc, FusionSelectedSubjectConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, LookupError):
        raise HTTPException(
            status_code=404, detail={"code": "fusion_not_found", "message": str(exc)}
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422, detail={"code": "invalid_fusion_decision", "message": str(exc)}
        ) from exc
    raise exc


def _board_view(board: FusionBoard) -> FusionBoardView:
    return FusionBoardView(
        edition_id=board.edition_id,
        snapshot_id=board.snapshot_id,
        snapshot_version=board.snapshot_version,
        read_only=board.read_only,
        candidate_count=board.candidate_count,
        group_count=board.group_count,
        pending_review_count=board.pending_review_count,
        groups=[
            FusionGroupView(
                discovery_subject_id=group.discovery_subject_id,
                title=group.title,
                summary=group.summary,
                candidate_ids=list(group.candidate_ids),
                candidates=[_candidate_view(item) for item in group.candidates],
                confidence=group.confidence,
                origin=group.origin,
                resolution_state=group.resolution_state,
                deterministic_signals=_signal_views(group.deterministic_signals),
                model_suggestion=_suggestion_view(group.model_suggestion),
                differences=list(group.differences),
                history=[
                    FusionHistoryView(
                        action=entry.action,
                        merge_run_id=entry.merge_run_id,
                        planner_kind=entry.planner_kind,
                        actor_id=entry.actor_id,
                        candidate_ids=list(entry.candidate_ids),
                        created_at=entry.created_at,
                    )
                    for entry in group.history
                ],
            )
            for group in board.groups
        ],
        pending_reviews=[
            FusionPendingReviewView(
                merge_run_id=review.merge_run_id,
                candidate_ids=list(review.candidate_ids),
                discovery_subject_ids=list(review.discovery_subject_ids),
                review_reasons=list(review.review_reasons),
                stale=review.stale,
                groups=[
                    FusionReviewGroupView(
                        candidate_ids=list(group.candidate_ids),
                        candidates=[_candidate_view(item) for item in group.candidates],
                        proposed_discovery_subject_ids=list(group.proposed_discovery_subject_ids),
                        confidence=group.confidence,
                        requires_decision=group.requires_decision,
                        deterministic_signals=_signal_views(group.deterministic_signals),
                        model_suggestion=_suggestion_view(group.model_suggestion),
                        differences=list(group.differences),
                    )
                    for group in review.groups
                ],
                created_at=review.created_at,
            )
            for review in board.pending_reviews
        ],
        unstabilized_candidates=[_candidate_view(item) for item in board.unstabilized_candidates],
    )


def _candidate_view(candidate: FusionCandidate) -> FusionCandidateView:
    return FusionCandidateView(
        id=candidate.id,
        discovery_run_id=candidate.discovery_run_id,
        discovery_batch_id=candidate.discovery_batch_id,
        supersedes_candidate_id=candidate.supersedes_candidate_id,
        title=candidate.title,
        summary=candidate.summary,
        event_date=candidate.event_date,
        actors=list(candidate.actors),
        campaigns=list(candidate.campaigns),
        malware=list(candidate.malware),
        cves=list(candidate.cves),
        iocs=list(candidate.iocs),
        countries=list(candidate.countries),
        sectors=list(candidate.sectors),
        likely_artifacts=list(candidate.likely_artifacts),
        publications=[
            FusionPublicationView(
                id=publication.id,
                url=publication.url,
                canonical_url=publication.canonical_url,
                title=publication.title,
                publisher=publication.publisher,
                published_at=publication.published_at,
            )
            for publication in candidate.publications
        ],
    )


def _signal_views(signals: tuple[FusionDeterministicSignal, ...]) -> list[FusionSignalView]:
    return [
        FusionSignalView(
            kind=signal.kind, value=signal.value, candidate_ids=list(signal.candidate_ids)
        )
        for signal in signals
    ]


def _suggestion_view(value: FusionModelSuggestion | None) -> FusionSuggestionView | None:
    if value is None:
        return None
    return FusionSuggestionView(recommendation=value.recommendation, summary=value.summary)

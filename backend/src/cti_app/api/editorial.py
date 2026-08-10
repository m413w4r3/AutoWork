from __future__ import annotations

from datetime import date
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.editorial import (
    EditorialActionError,
    EditorialBoard,
    EditorialGroupingService,
    EditorialGroupNotFoundError,
)
from cti_app.application.identity import IdentityProvider
from cti_app.domain.discovery import SourceRelationshipStatus
from cti_app.domain.editorial import (
    EditorialGroup,
    EditorialGroupStatus,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecision,
    HumanDecisionType,
)
from cti_app.logging import get_correlation_id

router = APIRouter(
    prefix="/api/editions/{edition_id}/editorial-groups",
    tags=["editorial-groups"],
)


class ScoreView(BaseModel):
    impact: int
    novelty: int
    technical_depth: int
    hunting_potential: int
    actionability: int
    source_quality: int
    total: int
    justifications: dict[str, str]


class CandidateSummaryView(BaseModel):
    id: UUID
    batch_id: UUID
    title: str
    summary: str
    event_date: date | None
    source_urls: list[str]


class HistoricalComparisonView(BaseModel):
    group_id: UUID
    title: str
    editorial_type: EditorialType | None
    subject_id: UUID | None


class EditorialGroupView(BaseModel):
    id: UUID
    edition_id: UUID
    title: str
    outcome: GroupingOutcome
    status: EditorialGroupStatus
    editorial_type: EditorialType | None
    subject_id: UUID | None
    candidates: list[CandidateSummaryView]
    score: ScoreView
    source_relationship_status: SourceRelationshipStatus
    needs_source_verification: bool
    needs_source_expansion: bool
    grouping_confidence: GroupingConfidence
    grouping_justification: str
    historical_comparison: HistoricalComparisonView | None
    version: int


class EditorialBoardView(BaseModel):
    groups: list[EditorialGroupView]
    selected_briefs: int
    selected_major: int
    target_briefs: int
    target_major: int
    automatic_selection: bool = False


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_ids: list[UUID] = Field(min_length=2, max_length=100)


class SplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[UUID] = Field(min_length=1, max_length=100)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2_000)


class SelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    editorial_type: EditorialType


class HumanDecisionView(BaseModel):
    id: UUID
    decision_type: HumanDecisionType
    group_ids: list[UUID]
    actor_id: str
    correlation_id: str
    payload: dict[str, object]
    occurred_at: str


@router.get("", response_model=EditorialBoardView)
async def read_board(edition_id: UUID, request: Request) -> EditorialBoardView:
    try:
        return _board_view(await _service(request).board(edition_id))
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/refresh", response_model=EditorialBoardView)
async def refresh_board(edition_id: UUID, request: Request) -> EditorialBoardView:
    try:
        await _service(request).synchronize(edition_id, resolve_ambiguous=False)
        return _board_view(await _service(request).board(edition_id))
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/merge", response_model=EditorialBoardView)
async def merge_groups(
    edition_id: UUID, payload: MergeRequest, request: Request
) -> EditorialBoardView:
    service, actor_id = await _runtime(request)
    try:
        await service.merge(
            edition_id,
            tuple(payload.group_ids),
            actor_id=actor_id,
            correlation_id=get_correlation_id(),
        )
        return _board_view(await service.board(edition_id))
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/{group_id}/split", response_model=EditorialBoardView)
async def split_group(
    edition_id: UUID, group_id: UUID, payload: SplitRequest, request: Request
) -> EditorialBoardView:
    service, actor_id = await _runtime(request)
    try:
        await service.split(
            edition_id,
            group_id,
            tuple(payload.candidate_ids),
            actor_id=actor_id,
            correlation_id=get_correlation_id(),
        )
        return _board_view(await service.board(edition_id))
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/{group_id}/reject", response_model=EditorialBoardView)
async def reject_group(
    edition_id: UUID, group_id: UUID, payload: RejectRequest, request: Request
) -> EditorialBoardView:
    service, actor_id = await _runtime(request)
    try:
        await service.reject(
            edition_id,
            group_id,
            reason=payload.reason,
            actor_id=actor_id,
            correlation_id=get_correlation_id(),
        )
        return _board_view(await service.board(edition_id))
    except Exception as exc:
        _raise_api_error(exc)


@router.post("/{group_id}/select", response_model=EditorialBoardView)
async def select_group(
    edition_id: UUID, group_id: UUID, payload: SelectRequest, request: Request
) -> EditorialBoardView:
    service, actor_id = await _runtime(request)
    try:
        await service.select(
            edition_id,
            group_id,
            payload.editorial_type,
            actor_id=actor_id,
            correlation_id=get_correlation_id(),
        )
        return _board_view(await service.board(edition_id))
    except Exception as exc:
        _raise_api_error(exc)


@router.get("/decisions", response_model=list[HumanDecisionView])
async def decisions(edition_id: UUID, request: Request) -> list[HumanDecisionView]:
    return [_decision_view(item) for item in await _service(request).decisions(edition_id)]


def _service(request: Request) -> EditorialGroupingService:
    service: EditorialGroupingService = request.app.state.editorial_service
    return service


async def _runtime(request: Request) -> tuple[EditorialGroupingService, str]:
    provider: IdentityProvider = request.app.state.identity_provider
    return _service(request), (await provider.current()).actor_id


def _board_view(board: EditorialBoard) -> EditorialBoardView:
    return EditorialBoardView(
        groups=[_group_view(group, board) for group in board.groups],
        selected_briefs=board.selected_briefs,
        selected_major=board.selected_major,
        target_briefs=board.target_briefs,
        target_major=board.target_major,
    )


def _group_view(group: EditorialGroup, board: EditorialBoard) -> EditorialGroupView:
    historical = (
        board.historical_groups.get(group.potential_historical_group_id)
        if group.potential_historical_group_id
        else None
    )
    return EditorialGroupView(
        id=group.id,
        edition_id=group.edition_id,
        title=group.title,
        outcome=group.outcome,
        status=group.status,
        editorial_type=group.editorial_type,
        subject_id=group.subject_id,
        candidates=[
            CandidateSummaryView(
                id=candidate.id,
                batch_id=reference.batch_id,
                title=candidate.title,
                summary=candidate.summary,
                event_date=candidate.event_date,
                source_urls=[source.canonical_url for source in candidate.sources],
            )
            for reference in group.candidate_references
            if (candidate := board.candidates.get(reference)) is not None
        ],
        score=ScoreView(
            impact=group.score.impact,
            novelty=group.score.novelty,
            technical_depth=group.score.technical_depth,
            hunting_potential=group.score.hunting_potential,
            actionability=group.score.actionability,
            source_quality=group.score.source_quality,
            total=group.score.total,
            justifications=group.score.justifications,
        ),
        source_relationship_status=group.source_relationship_status,
        needs_source_verification=group.needs_source_verification,
        needs_source_expansion=group.needs_source_expansion,
        grouping_confidence=group.grouping_confidence,
        grouping_justification=group.grouping_justification,
        historical_comparison=(
            HistoricalComparisonView(
                group_id=historical.id,
                title=historical.title,
                editorial_type=historical.editorial_type,
                subject_id=historical.subject_id,
            )
            if historical
            else None
        ),
        version=group.version,
    )


def _decision_view(decision: HumanDecision) -> HumanDecisionView:
    return HumanDecisionView(
        id=decision.id,
        decision_type=decision.decision_type,
        group_ids=list(decision.group_ids),
        actor_id=decision.actor_id,
        correlation_id=decision.correlation_id,
        payload=decision.payload,
        occurred_at=decision.occurred_at.isoformat(),
    )


def _raise_api_error(exc: Exception) -> NoReturn:
    if isinstance(exc, EditorialGroupNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Editorial group not found"
        )
    if isinstance(exc, (EditorialActionError, ValueError)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise exc

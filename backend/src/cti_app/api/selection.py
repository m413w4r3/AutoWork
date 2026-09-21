from __future__ import annotations

from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.identity import IdentityProvider
from cti_app.application.selection import (
    SelectionBoard,
    SelectionDecisionCommand,
    SelectionDecisionStaleError,
    SelectionEditionArchivedError,
    SelectionError,
    SelectionIdempotencyConflictError,
    SelectionInvalidCommandError,
    SelectionItem,
    SelectionLastDecision,
    SelectionNotFoundError,
    SelectionRecommendation,
    SelectionService,
    SelectionSnapshotStaleError,
    SelectionSubjectAlreadyMaterializedError,
)
from cti_app.domain.selection import SelectionAction
from cti_app.logging import get_correlation_id

selection_router = APIRouter(prefix="/api/editions/{edition_id}/selection", tags=["selection"])


class SelectionRecommendationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended: bool
    reason: str | None


class SelectionLastDecisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    action: SelectionAction
    snapshot_id: UUID
    snapshot_version: int
    subject_id: UUID | None
    actor_id: str
    occurred_at: datetime


class SelectionItemView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovery_subject_id: UUID
    canonical_discovery_subject_id: UUID
    title: str
    summary: str
    actor_or_campaign: str
    technical_potential: int
    technical_potential_reason: str
    artifacts: list[str]
    publications: list[Any]
    provisional_iocs: list[Any]
    uncertainties: list[str]
    selectable: bool
    blocking_reason: str | None
    effective_state: str
    subject_id: UUID | None
    recommendation: SelectionRecommendationView
    last_decision: SelectionLastDecisionView | None
    updated_since_decision: bool
    member_candidate_ids: list[UUID]


class SelectionBoardView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edition_id: UUID
    snapshot_id: UUID | None
    snapshot_version: int | None
    items: list[SelectionItemView]
    fusion_review_count: int
    selected: int
    ignored: int
    undecided: int


class SelectionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovery_subject_id: UUID
    action: SelectionAction
    expected_decision_id: UUID | None = None


class SelectionDecisionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_version: int = Field(ge=1)
    decisions: list[SelectionDecisionRequest] = Field(min_length=1)


@selection_router.get("", response_model=SelectionBoardView)
async def read_selection_board(edition_id: UUID, request: Request) -> SelectionBoardView:
    service: SelectionService = request.app.state.selection_service
    try:
        board = await service.board(edition_id)
    except Exception as exc:
        _raise_selection_error(exc)
    return _board_view(board)


@selection_router.post("/decisions", response_model=SelectionBoardView)
async def decide_selection(
    edition_id: UUID,
    payload: SelectionDecisionsRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SelectionBoardView:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "selection_idempotency_key_required",
                "message": "Idempotency-Key is required and must not be empty.",
            },
        )

    provider: IdentityProvider = request.app.state.identity_provider
    try:
        identity = await provider.current()
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "identity_not_found", "message": "Identity not found."},
        ) from exc

    correlation_id = get_correlation_id()
    commands = tuple(
        SelectionDecisionCommand(
            discovery_subject_id=decision.discovery_subject_id,
            action=decision.action,
            expected_decision_id=decision.expected_decision_id,
            actor_id=identity.actor_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key.strip(),
        )
        for decision in payload.decisions
    )
    service: SelectionService = request.app.state.selection_service
    try:
        board = await service.decide_many(
            edition_id,
            commands,
            snapshot_version=payload.snapshot_version,
        )
    except Exception as exc:
        _raise_selection_error(exc)
    return _board_view(board)


def _raise_selection_error(exc: Exception) -> NoReturn:
    if isinstance(
        exc,
        (
            SelectionSnapshotStaleError,
            SelectionDecisionStaleError,
            SelectionSubjectAlreadyMaterializedError,
            SelectionEditionArchivedError,
            SelectionIdempotencyConflictError,
        ),
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, SelectionNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": "selection_not_found", "message": str(exc)},
        ) from exc
    if isinstance(exc, SelectionInvalidCommandError):
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, SelectionError):
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    raise HTTPException(
        status_code=500,
        detail={"code": "selection_error", "message": "Selection request failed."},
    ) from exc


def _board_view(board: SelectionBoard) -> SelectionBoardView:
    return SelectionBoardView(
        edition_id=board.edition_id,
        snapshot_id=board.snapshot_id,
        snapshot_version=board.snapshot_version,
        items=[_item_view(item) for item in board.items],
        fusion_review_count=board.fusion_review_count,
        selected=board.selected,
        ignored=board.ignored,
        undecided=board.undecided,
    )


def _item_view(item: SelectionItem) -> SelectionItemView:
    return SelectionItemView(
        discovery_subject_id=item.discovery_subject_id,
        canonical_discovery_subject_id=item.canonical_discovery_subject_id,
        title=item.title,
        summary=item.summary,
        actor_or_campaign=item.actor_or_campaign,
        technical_potential=item.technical_potential,
        technical_potential_reason=item.technical_potential_reason,
        artifacts=list(item.artifacts),
        publications=list(item.publications),
        provisional_iocs=list(item.provisional_iocs),
        uncertainties=list(item.uncertainties),
        selectable=item.selectable,
        blocking_reason=item.blocking_reason,
        effective_state=item.effective_state,
        subject_id=item.subject_id,
        recommendation=_recommendation_view(item.recommendation),
        last_decision=_last_decision_view(item.last_decision),
        updated_since_decision=item.updated_since_decision,
        member_candidate_ids=list(item.member_candidate_ids),
    )


def _recommendation_view(recommendation: SelectionRecommendation) -> SelectionRecommendationView:
    return SelectionRecommendationView(
        recommended=recommendation.recommended,
        reason=recommendation.reason,
    )


def _last_decision_view(
    decision: SelectionLastDecision | None,
) -> SelectionLastDecisionView | None:
    if decision is None:
        return None
    return SelectionLastDecisionView(
        id=decision.id,
        action=decision.action,
        snapshot_id=decision.snapshot_id,
        snapshot_version=decision.snapshot_version,
        subject_id=decision.subject_id,
        actor_id=decision.actor_id,
        occurred_at=decision.occurred_at,
    )

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date
from typing import Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.editorial import (
    EditorialActionError,
    EditorialBoard,
    EditorialDecisionCommand,
    EditorialDecisionValue,
    EditorialGroupingService,
    EditorialGroupNotFoundError,
)
from cti_app.application.identity import IdentityProvider
from cti_app.domain.discovery import (
    ProvisionalDiscoveryIoc,
    SourceCandidate,
    SourceRelationshipStatus,
)
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


class EditorialPublicationView(BaseModel):
    title: str
    url: str
    publisher: str | None
    role: str
    published_at: date | None


class ProvisionalIocView(BaseModel):
    raw_value: str
    normalized_value: str | None
    proposed_type: str
    declared_type: str | None
    warnings: list[str]


class EditorialGroupView(BaseModel):
    id: UUID
    edition_id: UUID
    title: str
    outcome: GroupingOutcome
    status: EditorialGroupStatus
    editorial_type: EditorialType | None
    subject_id: UUID | None
    presentation: str | None
    actor_or_campaign: str | None
    technical_potential: int
    technical_potential_reason: str | None
    artifacts: list[str]
    publications: list[EditorialPublicationView]
    uncertainties: list[str]
    publisher_ioc_count_total: int | None
    publisher_ioc_counts: list[int]
    provisional_ioc_count: int
    provisional_ioc_type_counts: dict[str, int]
    provisional_iocs: list[ProvisionalIocView]
    metadata_incomplete: bool
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
    ignored: int
    undecided: int
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


class EditorialDecisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: UUID
    version: int = Field(ge=1)
    decision: Literal["brief", "major", "ignore"]


class EditorialDecisionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[EditorialDecisionItem] = Field(min_length=1, max_length=500)


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


@router.post("/decisions", response_model=EditorialBoardView)
async def apply_decisions(
    edition_id: UUID, payload: EditorialDecisionsRequest, request: Request
) -> EditorialBoardView:
    service, actor_id = await _runtime(request)
    try:
        await service.decide_many(
            edition_id,
            tuple(
                EditorialDecisionCommand(
                    group_id=item.group_id,
                    version=item.version,
                    decision=EditorialDecisionValue(item.decision),
                )
                for item in payload.decisions
            ),
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
        ignored=board.ignored,
        undecided=board.undecided,
        target_briefs=board.target_briefs,
        target_major=board.target_major,
    )


def _group_view(group: EditorialGroup, board: EditorialBoard) -> EditorialGroupView:
    historical = (
        board.historical_groups.get(group.potential_historical_group_id)
        if group.potential_historical_group_id
        else None
    )
    candidates = [
        candidate
        for reference in group.candidate_references
        if (candidate := board.candidates.get(reference)) is not None
    ]
    representative = max(candidates, key=lambda item: item.technical_potential, default=None)
    actor_values = _meaningful_values(
        value
        for candidate in candidates
        for value in (
            candidate.actor_or_campaign,
            *candidate.actors,
            *candidate.campaigns,
        )
    )
    artifacts = _meaningful_values(
        item for candidate in candidates for item in candidate.likely_artifacts
    )
    uncertainties = _meaningful_values(
        item for candidate in candidates for item in candidate.uncertainties
    )

    role_order = {"primary": 0, "independent": 1, "relay": 2, "aggregator": 3}
    publications_by_url: dict[str, SourceCandidate] = {}
    for candidate in candidates:
        for source in candidate.sources:
            previous = publications_by_url.get(source.canonical_url)
            if previous is None or role_order.get(
                source.role.value, 9
            ) < role_order.get(previous.role.value, 9):
                publications_by_url[source.canonical_url] = source
    publications = sorted(
        publications_by_url.values(),
        key=lambda source: (
            role_order.get(source.role.value, 9),
            source.published_at is None,
            source.published_at or date.min,
            source.title.casefold(),
        ),
    )

    provisional_by_value: dict[str, ProvisionalDiscoveryIoc] = {}
    for candidate in candidates:
        for ioc in candidate.provisional_iocs:
            key = (ioc.normalized_value or ioc.raw_value).casefold()
            provisional_by_value.setdefault(key, ioc)
    provisional_iocs = list(provisional_by_value.values())
    type_counts = Counter(item.proposed_type.value for item in provisional_iocs)
    declared_counts = sorted(
        {
            source.ioc_declared_count
            for source in publications
            if source.ioc_declared_count is not None
        }
    )
    presentation = next((item.summary for item in candidates if item.summary.strip()), None)
    actor_or_campaign = " · ".join(actor_values) if actor_values else None
    technical_reason = (
        representative.technical_potential_reason
        if representative
        and _is_meaningful(representative.technical_potential_reason)
        else None
    )
    metadata_incomplete = not all(
        (presentation, actor_or_campaign, publications, technical_reason)
    )

    return EditorialGroupView(
        id=group.id,
        edition_id=group.edition_id,
        title=group.title,
        outcome=group.outcome,
        status=group.status,
        editorial_type=group.editorial_type,
        subject_id=group.subject_id,
        presentation=presentation,
        actor_or_campaign=actor_or_campaign,
        technical_potential=(representative.technical_potential if representative else 0),
        technical_potential_reason=technical_reason,
        artifacts=artifacts,
        publications=[
            EditorialPublicationView(
                title=source.title,
                url=source.canonical_url,
                publisher=source.publisher if _is_meaningful(source.publisher) else None,
                role=source.role.value,
                published_at=source.published_at,
            )
            for source in publications
        ],
        uncertainties=uncertainties,
        publisher_ioc_count_total=(declared_counts[0] if len(declared_counts) == 1 else None),
        publisher_ioc_counts=declared_counts,
        provisional_ioc_count=len(provisional_iocs),
        provisional_ioc_type_counts=dict(sorted(type_counts.items())),
        provisional_iocs=[
            ProvisionalIocView(
                raw_value=item.raw_value,
                normalized_value=item.normalized_value,
                proposed_type=item.proposed_type.value,
                declared_type=(
                    item.declared_type if _is_meaningful(item.declared_type) else None
                ),
                warnings=list(item.warnings),
            )
            for item in provisional_iocs
        ],
        metadata_incomplete=metadata_incomplete,
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


def _is_meaningful(value: str) -> bool:
    return bool(value.strip()) and value.strip().casefold() not in {"unknown", "none"}


def _meaningful_values(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not _is_meaningful(value):
            continue
        key = value.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


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

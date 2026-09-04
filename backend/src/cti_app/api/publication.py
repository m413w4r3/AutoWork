"""HTTP API for the append-only edition publication review checkpoint."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cti_app.api.production import (
    ProductionReconciliationView,
    RetryProductionStageRequest,
    _retry_production_run,
    reconciliation_view,
)
from cti_app.application.collection import SupplementalSource
from cti_app.application.collection_errors import CollectionNotAllowedError
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
    EditionRepairItem,
    EditionRepairReadService,
    EditionReview,
    EditionReviewItemNotFoundError,
    EditionReviewNotFoundError,
    EditionReviewService,
    EditionReviewStatusError,
    InvalidReviewDocumentError,
    InvalidReviewReasonError,
    ReviewItemStaleError,
    issue_application_state,
)
from cti_app.application.identity import IdentityProvider
from cti_app.application.production_repairs import (
    ProductionReferenceRepairError,
    ProductionReferenceRepairService,
    ProductionRepairActionInvalidError,
    ProductionRepairAdjudicationRequest,
    ProductionRepairAdjudicationService,
    ProductionRepairDecisionChangedError,
    ProductionRepairDecisionNoopError,
    ProductionRepairIssueNotFoundError,
    ProductionRepairIssueService,
    ProductionRepairProjectionError,
    ProductionRepairProjectionService,
    ProductionRepairStaleError,
    ProductionRepairStatusError,
    ProductionRepairValueNotVerifiableError,
)
from cti_app.domain.jobs import JobStatus
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionReconciliationRequiredError,
    ProductionRepairAction,
    ProductionRepairIssueKind,
    RepairDecisionApplicationState,
    SubjectProductionStage,
    SubjectProductionStatus,
)
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
    rejected_ioc_count: int = 0
    rejected_other_artifact_count: int = 0
    rejected_rule_count: int = 0
    published_rule_count: int = 0
    active_repair_count: int = 0
    unresolved_repair_count: int = 0
    pending_rebuild_count: int = 0
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
    unresolved_repair_count: int = 0
    repair_review_complete: bool = True
    pending_rebuild_count: int = 0


class EditionRepairDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["include", "exclude", "continue_without_source"]
    observed_subject_id: UUID
    observed_run_id: UUID
    observed_artifact_id: UUID
    observed_pipeline_generation: Annotated[int, Field(ge=0)]
    # Optimistic fence: null for a first decision, the displayed decision id
    # when the analyst revises an existing one.
    expected_effective_decision_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=500)


class EditionRepairBulkDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_key: str
    action: Literal["include", "exclude", "continue_without_source"]
    observed_subject_id: UUID
    observed_run_id: UUID
    observed_artifact_id: UUID
    observed_pipeline_generation: Annotated[int, Field(ge=0)]
    expected_effective_decision_id: UUID | None = None


class EditionRepairBulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[EditionRepairBulkDecision] = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=500)


class EditionRepairRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_run_id: UUID | None = None
    observed_pipeline_generation: Annotated[int, Field(ge=0)] | None = None
    observed_artifact_id: UUID | None = None


class EditionRepairItemView(BaseModel):
    repair_key: str
    kind: str
    position: int
    subject_id: UUID
    article_title: str
    run_id: UUID
    pipeline_generation: int
    artifact_id: UUID | None
    artifact_version: int | None
    source_id: str | None
    source_title: str | None
    source_url: str | None
    collection_id: UUID | None
    collection_state: str | None
    artifact_type: str | None
    preview: str
    reason_code: str
    value_sha256: str
    payload_available: bool
    effective_action: str | None
    effective_decision_id: UUID | None
    resolved: bool
    resolution_reason: str | None
    rebuild_required: bool
    recommended_stage: str | None
    repair_state: str | None = None
    is_publication_ioc: bool
    in_publication_scope: bool = True
    application_state: str = RepairDecisionApplicationState.UNRESOLVED.value


class EditionRepairSummaryView(BaseModel):
    unresolved_total: int
    sources_to_supply: int
    rejected_iocs_to_review: int
    rejected_rules_to_review: int
    rejected_other_artifacts: int
    articles_with_repairs: int
    articles_needing_rebuild: int


class EditionRepairArticleView(BaseModel):
    subject_id: UUID
    has_pending_projection: bool
    recommended_stage: str
    active_repair_count: int
    resolved_since_last_build_count: int


class EditionRepairPageView(BaseModel):
    summary: EditionRepairSummaryView
    items: list[EditionRepairItemView]
    articles: list[EditionRepairArticleView]
    next_cursor: str | None


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


def _repair_issue_service(request: Request) -> ProductionRepairIssueService:
    configured = getattr(request.app.state, "production_repair_issue_service", None)
    if configured is not None:
        return cast(ProductionRepairIssueService, configured)
    return ProductionRepairIssueService(
        request.app.state.uow_factory,
        getattr(request.app.state, "production_artifact_store", None),
    )


def _repair_read_service(request: Request) -> EditionRepairReadService:
    configured = getattr(request.app.state, "edition_repair_read_service", None)
    if configured is not None:
        return cast(EditionRepairReadService, configured)
    return EditionRepairReadService(
        request.app.state.uow_factory,
        _repair_issue_service(request),
    )


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


@router.get(
    "/editions/{edition_id}/review/repairs",
    response_model=EditionRepairPageView,
)
async def get_edition_review_repairs(
    edition_id: UUID,
    request: Request,
    status_filter: Literal["open", "resolved", "all"] = Query("open", alias="status"),
    kind: ProductionRepairIssueKind | None = Query(default=None),
    subject_id: UUID | None = Query(default=None),
    artifact_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
) -> EditionRepairPageView:
    try:
        page = await _repair_read_service(request).list(
            edition_id,
            status=status_filter,
            kind=kind,
            subject_id=subject_id,
            artifact_type=artifact_type,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        if str(exc) == "invalid_repair_cursor":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_repair_cursor"},
            ) from exc
        _raise_review_error(exc)
    return EditionRepairPageView(
        summary=EditionRepairSummaryView(
            unresolved_total=page.summary.unresolved_total,
            sources_to_supply=page.summary.sources_to_supply,
            rejected_iocs_to_review=page.summary.rejected_iocs_to_review,
            rejected_rules_to_review=page.summary.rejected_rules_to_review,
            rejected_other_artifacts=page.summary.rejected_other_artifacts,
            articles_with_repairs=page.summary.articles_with_repairs,
            articles_needing_rebuild=page.summary.articles_needing_rebuild,
        ),
        items=[EditionRepairItemView(**_repair_item_view(item)) for item in page.items],
        articles=[
            EditionRepairArticleView(
                subject_id=item.subject_id,
                has_pending_projection=item.has_pending_projection,
                recommended_stage=item.recommended_stage,
                active_repair_count=item.active_repair_count,
                resolved_since_last_build_count=item.resolved_since_last_build_count,
            )
            for item in page.articles
        ],
        next_cursor=page.next_cursor,
    )


async def _edition_repair_issues(request: Request, edition_id: UUID) -> tuple[Any, ...]:
    service = _repair_issue_service(request)
    getter = getattr(service, "list_issue_views", None)
    extraction = (
        await getter(edition_id) if callable(getter) else await service.list_issues(edition_id)
    )
    supplemental_getter = getattr(service, "list_supplemental_source_issues", None)
    supplemental = await supplemental_getter(edition_id) if callable(supplemental_getter) else ()
    return tuple([*extraction, *supplemental])


def _repair_issue_key(issue: Any) -> str:
    return str(issue.repair_key)


def _repair_issue_subject(issue: Any) -> UUID | None:
    value = getattr(issue, "subject_id", None)
    return value if isinstance(value, UUID) else None


def _repair_issue_kind(issue: Any) -> ProductionRepairIssueKind:
    value = getattr(issue, "kind", None)
    normalized = getattr(value, "value", value)
    if not isinstance(normalized, str):
        raise ValueError("production_repair_issue_kind_invalid")
    return ProductionRepairIssueKind(normalized)


async def _find_edition_repair_issue(
    request: Request, edition_id: UUID, repair_key: str
) -> Any | None:
    for issue in await _edition_repair_issues(request, edition_id):
        if _repair_issue_key(issue) == repair_key:
            return issue
    return None


def _edition_repair_error(exc: Exception, repair_key: str | None = None) -> NoReturn:
    # A batch failure must name the item that refused, not just the batch.
    repair_key = repair_key or getattr(exc, "repair_key", None)
    if isinstance(exc, ProductionRepairIssueNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_repair_detail(ProductionRepairIssueNotFoundError.code, repair_key),
        ) from exc
    if isinstance(exc, ProductionRepairValueNotVerifiableError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_repair_detail(ProductionRepairValueNotVerifiableError.code, repair_key),
        ) from exc
    if isinstance(exc, ProductionRepairDecisionChangedError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_repair_detail(ProductionRepairDecisionChangedError.code, repair_key),
        ) from exc
    if isinstance(exc, ProductionRepairDecisionNoopError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_repair_detail(ProductionRepairDecisionNoopError.code, repair_key),
        ) from exc
    if isinstance(exc, ProductionRepairActionInvalidError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_repair_detail(ProductionRepairActionInvalidError.code, repair_key),
        ) from exc
    if isinstance(exc, ProductionRepairStaleError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_repair_detail(ProductionRepairStaleError.code, repair_key),
        ) from exc
    if isinstance(exc, ProductionRepairStatusError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": str(exc)},
        ) from exc
    if str(exc) in {
        "production_repair_bulk_empty",
        "production_repair_bulk_limit_exceeded",
        "production_repair_duplicate",
        "Production repair action is incompatible with issue kind",
    }:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": (
                    "production_repair_action_invalid" if "incompatible" in str(exc) else str(exc)
                )
            },
        ) from exc
    raise exc


def _repair_detail(code: str, repair_key: str | None) -> dict[str, str]:
    return {"code": code} | ({"repair_key": repair_key} if repair_key else {})


def _repair_application_state(issue: Any | None) -> str:
    """Say what the current projection materializes, never what was decided.

    An arbitration and a deliverable are two different facts: the audit must
    never read "the analyst decided INCLUDE" as "the artifact contains it".
    """
    if issue is None:
        return RepairDecisionApplicationState.UNRESOLVED.value
    return issue_application_state(issue, getattr(issue, "effective_decision", None)).value


def _production_repair_decision_history_view(
    history: Sequence[Any],
) -> list[dict[str, Any]]:
    """Expose the complete append-only audit in chronological order."""
    return [
        view
        for view in (_production_repair_decision_view(item) for item in history)
        if view is not None
    ]


@router.get("/editions/{edition_id}/review/repairs/{repair_key}")
async def get_edition_review_repair_detail(
    edition_id: UUID, repair_key: str, request: Request
) -> dict[str, Any]:
    issue = await _find_edition_repair_issue(request, edition_id, repair_key)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "production_repair_issue_not_found"},
        )
    subject_id = _repair_issue_subject(issue)
    service = _repair_issue_service(request)
    if _repair_issue_kind(issue) is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED:
        source_detail = await service.get_supplemental_source_issue(
            edition_id, repair_key, subject_id
        )
        result = {
            "repair_key": repair_key,
            "kind": _repair_issue_kind(issue).value,
            "source_id": getattr(source_detail, "source_id", None) if source_detail else None,
            "source_title": getattr(source_detail, "source_title", None) if source_detail else None,
            "source_url": getattr(source_detail, "source_url", None) if source_detail else None,
            "publisher": getattr(source_detail, "publisher", None) if source_detail else None,
            "collection_id": (
                str(source_detail.collection_id)
                if source_detail is not None and source_detail.collection_id is not None
                else None
            ),
            "collection_state": getattr(source_detail, "collection_state", None)
            if source_detail
            else None,
            "repair_state": _repair_state_value(source_detail),
            "rebuild_required": bool(
                getattr(source_detail, "rebuild_required", False) if source_detail else False
            ),
            "recommended_action": getattr(source_detail, "recommended_action", None)
            if source_detail
            else None,
            "effective_decision": _production_repair_decision_view(
                getattr(source_detail, "effective_decision", None) if source_detail else None
            ),
            "application_state": _repair_application_state(source_detail),
            "decision_history": _production_repair_decision_history_view(
                await _repair_adjudication_service(request).decision_history(
                    edition_id, repair_key, subject_id
                )
            ),
        }
        return result

    issue_detail = await service.get_issue(edition_id, repair_key, subject_id)
    if issue_detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "production_repair_issue_not_found"},
        )
    result = {
        "repair_key": repair_key,
        "kind": _repair_issue_kind(issue).value,
        "artifact_id": issue_detail.issue.observed_artifact_id,
        "artifact_version": issue_detail.issue.observed_artifact_version,
        "source_id": issue_detail.issue.source_id,
        "source_title": issue_detail.issue.source_title,
        "source_url": issue_detail.issue.source_url,
        "artifact_type": issue_detail.issue.artifact_type,
        "reason_code": issue_detail.issue.reason_code,
        "value_sha256": issue_detail.issue.value_sha256,
        "preview": issue_detail.issue.preview,
        "payload_available": issue_detail.payload_available,
        "value": issue_detail.value,
        "body": (
            issue_detail.value
            if issue_detail.issue.kind is ProductionRepairIssueKind.REJECTED_RULE
            else None
        ),
        "effective_decision": _production_repair_decision_view(
            issue_detail.issue.effective_decision
        ),
        "application_state": _repair_application_state(issue_detail.issue),
        "decision_history": _production_repair_decision_history_view(issue_detail.decision_history),
    }
    return result


@router.post("/editions/{edition_id}/review/repairs/{repair_key}/decision")
async def decide_edition_review_repair(
    edition_id: UUID,
    repair_key: str,
    payload: EditionRepairDecisionRequest,
    request: Request,
) -> dict[str, Any]:
    """Arbitrate one issue through the single adjudication policy.

    The router no longer owns any business rule: resolving the CURRENT issue,
    refusing an unbuildable INCLUDE and holding the optimistic fence all live
    in the service the subject-scoped endpoint calls too.
    """
    try:
        decision = await _repair_adjudication_service(request).decide_current_issue(
            edition_id=edition_id,
            subject_id=payload.observed_subject_id,
            repair_key=repair_key,
            action=ProductionRepairAction(payload.action),
            observed_run_id=payload.observed_run_id,
            observed_artifact_id=payload.observed_artifact_id,
            observed_pipeline_generation=payload.observed_pipeline_generation,
            expected_effective_decision_id=payload.expected_effective_decision_id,
            actor_id=await _actor_id(request),
            reason=payload.reason,
        )
    except Exception as exc:
        _edition_repair_error(exc, repair_key)
    return {
        "repair_key": repair_key,
        "decision_id": str(decision.id),
        "action": decision.action.value,
        "resolved": True,
    }


def _repair_adjudication_service(
    request: Request,
) -> ProductionRepairAdjudicationService:
    configured = getattr(request.app.state, "production_repair_adjudication_service", None)
    if configured is not None:
        return cast(ProductionRepairAdjudicationService, configured)
    return ProductionRepairAdjudicationService(
        request.app.state.uow_factory,
        _repair_issue_service(request),
        getattr(request.app.state, "production_repair_decision_service", None),
    )


@router.post("/editions/{edition_id}/review/repairs/decisions")
async def decide_edition_review_repairs(
    edition_id: UUID,
    payload: EditionRepairBulkRequest,
    request: Request,
) -> dict[str, Any]:
    # The batch carries no route-only prevalidation: the very same invariant
    # decides each item, and every append lands in one transaction, so a
    # single impossible INCLUDE leaves zero decisions behind.
    requests = [
        ProductionRepairAdjudicationRequest(
            subject_id=requested.observed_subject_id,
            repair_key=requested.repair_key,
            action=ProductionRepairAction(requested.action),
            observed_artifact_id=requested.observed_artifact_id,
            observed_pipeline_generation=requested.observed_pipeline_generation,
            expected_effective_decision_id=requested.expected_effective_decision_id,
            observed_run_id=requested.observed_run_id,
        )
        for requested in payload.decisions
    ]
    try:
        events = await _repair_adjudication_service(request).decide_current_issues(
            edition_id=edition_id,
            requests=requests,
            actor_id=await _actor_id(request),
            reason=payload.reason,
        )
    except Exception as exc:
        _edition_repair_error(exc)
    return {
        "decision_ids": [str(event.id) for event in events],
        "decisions": [
            {
                "repair_key": event.repair_key,
                "decision_id": str(event.id),
                "action": event.action.value,
            }
            for event in events
        ],
    }


def _repair_state_value(issue: Any) -> str | None:
    value = getattr(issue, "repair_state", None)
    if value is None:
        return None
    return str(getattr(value, "value", value))


@router.post("/editions/{edition_id}/review/repairs/{repair_key}/source")
async def prepare_edition_review_repair_source(
    edition_id: UUID, repair_key: str, request: Request
) -> dict[str, Any]:
    """Attach the SourceCollection a Q1 proposal never received.

    Q1 can name a publication the collection pass never registered. Without a
    collection there is nothing to upload content to, so the Repair Desk would
    show a dead end. This idempotent command creates -- or returns -- exactly
    the collection matching the raw Q1 source, through the same application
    primitive reference research uses. No model is contacted.
    """
    issue = await _find_edition_repair_issue(request, edition_id, repair_key)
    if issue is None or (
        _repair_issue_kind(issue) is not ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "production_repair_issue_not_found"},
        )
    subject_id = _repair_issue_subject(issue)
    if subject_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "production_repair_stale"},
        )
    service = getattr(request.app.state, "collection_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "collection_service_unavailable"},
        )
    try:
        collection = await service.ensure_supplemental_source(
            subject_id,
            SupplementalSource(
                url=str(issue.source_url),
                title=(str(issue.source_title) or None),
                publisher=getattr(issue, "publisher", None),
            ),
        )
    except CollectionNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "supplemental_source_not_attachable", "message": str(exc)},
        ) from exc
    return {
        "repair_key": repair_key,
        "subject_id": str(subject_id),
        "collection_id": str(collection.id),
        "collection_state": getattr(collection.state, "value", collection.state),
        "source_url": collection.canonical_url,
    }


def _reference_repair_service(request: Request) -> ProductionReferenceRepairService:
    configured = getattr(request.app.state, "production_reference_repair_service", None)
    return configured or ProductionReferenceRepairService(
        request.app.state.uow_factory,
        getattr(request.app.state, "production_artifact_store", None),
    )


def _repair_projection_service(request: Request) -> ProductionRepairProjectionService:
    configured = getattr(request.app.state, "production_repair_projection_service", None)
    return configured or ProductionRepairProjectionService(
        request.app.state.uow_factory,
        getattr(request.app.state, "production_artifact_store", None),
    )


async def _edition_subject_production_state(
    request: Request, edition_id: UUID, subject_id: UUID
) -> tuple[Any, dict[str, Any], UUID | None]:
    async with request.app.state.uow_factory() as uow:
        edition = await uow.editions.get(edition_id)
        if edition is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "edition_not_found"},
            )
        if getattr(edition.status, "value", edition.status) not in {"review", "production"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "edition_frozen_for_publication"},
            )
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if run is None or run.edition_id != edition_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "production_run_not_found"},
            )
        artifacts = await uow.production_artifacts.list_for_run(run.id)
        current: dict[str, Any] = {}
        for stage in ProductionArtifactStage:
            getter = getattr(uow.production_artifacts, "get_current", None)
            artifact = (
                await getter(run.id, stage.value)
                if callable(getter)
                else next(
                    (
                        item
                        for item in artifacts
                        if getattr(getattr(item, "stage", None), "value", item.stage) == stage.value
                        and getattr(getattr(item, "status", None), "value", item.status) != "stale"
                    ),
                    None,
                )
            )
            if artifact is not None:
                current[stage.value] = artifact
        batch_id = None
        get_by_run = getattr(uow.edition_production_batch_items, "get_by_run", None)
        if callable(get_by_run):
            item = await get_by_run(run.id)
            batch_id = getattr(item, "batch_id", None) if item else None
        return run, current, batch_id


def _rebuild_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProductionReconciliationRequiredError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "production_reconciliation_required"},
        )
    if isinstance(exc, (ProductionReferenceRepairError, ProductionRepairProjectionError)):
        code = getattr(exc, "code", None) or str(exc)
        return HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if code in {"production_run_not_found", "references_artifact_not_found"}
                else status.HTTP_409_CONFLICT
            ),
            detail={"code": code, "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": str(exc)},
    )


@router.post("/editions/{edition_id}/review/items/{subject_id}/rebuild")
async def rebuild_edition_review_item(
    edition_id: UUID,
    subject_id: UUID,
    request: Request,
    payload: EditionRepairRebuildRequest | None = None,
) -> dict[str, Any]:
    run, current, batch_id = await _edition_subject_production_state(
        request, edition_id, subject_id
    )
    if payload is not None and (
        (payload.observed_run_id is not None and payload.observed_run_id != run.id)
        or (
            payload.observed_pipeline_generation is not None
            and payload.observed_pipeline_generation != run.pipeline_generation
        )
        or (
            payload.observed_artifact_id is not None
            and payload.observed_artifact_id not in {artifact.id for artifact in current.values()}
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "production_repair_stale"},
        )

    repair_read_service = _repair_read_service(request)
    page = await repair_read_service.list(
        edition_id, status="all", subject_id=subject_id, limit=200
    )
    repair_items = list(page.items)
    while page.next_cursor is not None:
        page = await repair_read_service.list(
            edition_id,
            status="all",
            subject_id=subject_id,
            cursor=page.next_cursor,
            limit=200,
        )
        repair_items.extend(page.items)
    actionable_open = [
        item
        for item in repair_items
        if not item.resolved
        and (
            item.kind
            in {
                ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value,
                ProductionRepairIssueKind.REJECTED_RULE.value,
            }
            or (
                item.kind == ProductionRepairIssueKind.REJECTED_INDICATOR.value
                and item.is_publication_ioc
            )
        )
    ]
    if actionable_open:
        return {
            "action": "awaiting_repair_decision",
            "stage": None,
            "run_id": str(run.id),
            "batch_id": str(batch_id) if batch_id else None,
            "changed": False,
            "job_id": None,
        }

    actor_id = await _actor_id(request)
    q2_resolved = [
        item
        for item in repair_items
        if item.resolved
        and item.kind
        in {
            ProductionRepairIssueKind.REJECTED_INDICATOR.value,
            ProductionRepairIssueKind.REJECTED_RULE.value,
        }
    ]
    # A source whose content was supplied owes a REFERENCES reconciliation.
    # That rebuild stales EXTRACTION anyway, so projecting the Q2 decisions
    # first would only produce an artifact the next stage discards.
    references_pending = any(
        item.kind == ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value
        and item.rebuild_required
        for item in repair_items
    )
    try:
        if (
            not references_pending
            and q2_resolved
            and any(item.recommended_stage == "apply_projection" for item in q2_resolved)
        ):
            projection = await _repair_projection_service(request).project_effective_extraction(
                run.id, actor_id=actor_id
            )
            if projection.unresolved_count:
                return {
                    "action": "awaiting_repair_decision",
                    "stage": None,
                    "run_id": str(run.id),
                    "batch_id": str(batch_id) if batch_id else None,
                    "changed": False,
                    "job_id": None,
                }
            if projection.changed or current.get(ProductionArtifactStage.PUBLICATION.value) is None:
                retry = await _retry_production_run(
                    request,
                    run.id,
                    RetryProductionStageRequest(stage=SubjectProductionStage.SYNTHESIS),
                    actor_id,
                )
                return {
                    "action": "apply_projection_and_retry",
                    "stage": SubjectProductionStage.SYNTHESIS.value,
                    "run_id": str(retry.get("run_id", run.id)),
                    "batch_id": retry.get("batch_id"),
                    "changed": projection.changed,
                    "job_id": retry.get("job_id"),
                }
            return {
                "action": "none",
                "stage": "none",
                "run_id": str(run.id),
                "batch_id": str(batch_id) if batch_id else None,
                "changed": False,
                "job_id": None,
            }

        references = current.get(ProductionArtifactStage.REFERENCES.value)
        extraction = current.get(ProductionArtifactStage.EXTRACTION.value)
        synthesis = current.get(ProductionArtifactStage.SYNTHESIS.value)
        publication = current.get(ProductionArtifactStage.PUBLICATION.value)
        archived_source_urls: set[str] = set()
        async with request.app.state.uow_factory() as uow:
            collections = await uow.source_collections.list_for_subject(subject_id)
            archived_source_urls = {
                collection.canonical_url
                for collection in collections
                if getattr(getattr(collection, "state", None), "value", collection.state)
                in {"archived", "extracted", "completed"}
            }
        archived_sources = bool(archived_source_urls)
        reference_metadata = (
            getattr(references, "metadata", None) if references is not None else None
        )
        reference_is_derived = bool(
            isinstance(reference_metadata, dict) and reference_metadata.get("derived_repair")
        )
        indexed_canonical_urls: set[str] | None = None
        if isinstance(reference_metadata, dict):
            source_index = reference_metadata.get("repair_source_index")
            canonical_index = (
                source_index.get("canonical") if isinstance(source_index, dict) else None
            )
            if isinstance(canonical_index, list):
                indexed_canonical_urls = {
                    str(item.get("source_url"))
                    for item in canonical_index
                    if isinstance(item, dict) and item.get("source_url")
                }
        references_need_repair = archived_sources and (
            not reference_is_derived
            or (
                indexed_canonical_urls is not None
                and bool(archived_source_urls - indexed_canonical_urls)
            )
        )
        if references is not None and references_need_repair:
            result = await _reference_repair_service(request).rebuild_from_archived_q1(
                run.id, actor_id=actor_id
            )
            if result.changed:
                retry = await _retry_production_run(
                    request,
                    run.id,
                    RetryProductionStageRequest(stage=SubjectProductionStage.EXTRACTION),
                    actor_id,
                )
                return {
                    "action": "rebuild_references_and_retry",
                    "stage": SubjectProductionStage.EXTRACTION.value,
                    "run_id": str(retry.get("run_id", run.id)),
                    "batch_id": retry.get("batch_id"),
                    "changed": True,
                    "job_id": retry.get("job_id"),
                }
            if extraction is None:
                retry = await _retry_production_run(
                    request,
                    run.id,
                    RetryProductionStageRequest(stage=SubjectProductionStage.EXTRACTION),
                    actor_id,
                )
                return {
                    "action": "retry_stage",
                    "stage": SubjectProductionStage.EXTRACTION.value,
                    "run_id": str(retry.get("run_id", run.id)),
                    "batch_id": retry.get("batch_id"),
                    "changed": True,
                    "job_id": retry.get("job_id"),
                }
            return {
                "action": "rebuild_references",
                "stage": SubjectProductionStage.REFERENCES.value,
                "run_id": str(run.id),
                "batch_id": str(batch_id) if batch_id else None,
                "changed": False,
                "job_id": None,
            }
        next_stage = (
            SubjectProductionStage.REFERENCES
            if references is None
            else SubjectProductionStage.EXTRACTION
            if extraction is None
            else SubjectProductionStage.SYNTHESIS
            if synthesis is None
            else SubjectProductionStage.ASSEMBLY
            if publication is None
            else None
        )
        if next_stage is None:
            return {
                "action": "none",
                "stage": "none",
                "run_id": str(run.id),
                "batch_id": str(batch_id) if batch_id else None,
                "changed": False,
                "job_id": None,
            }
        retry = await _retry_production_run(
            request,
            run.id,
            RetryProductionStageRequest(stage=next_stage),
            actor_id,
        )
        return {
            "action": "retry_stage",
            "stage": next_stage.value,
            "run_id": str(retry.get("run_id", run.id)),
            "batch_id": retry.get("batch_id"),
            "changed": True,
            "job_id": retry.get("job_id"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _rebuild_error(exc) from exc


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
                rejected_ioc_count=item.rejected_ioc_count,
                rejected_other_artifact_count=item.rejected_other_artifact_count,
                rejected_rule_count=item.rejected_rule_count,
                published_rule_count=item.published_rule_count,
                active_repair_count=item.active_repair_count,
                unresolved_repair_count=item.unresolved_repair_count,
                pending_rebuild_count=item.pending_rebuild_count,
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
        unresolved_repair_count=review.unresolved_repair_count,
        repair_review_complete=review.repair_review_complete,
        pending_rebuild_count=review.pending_rebuild_count,
    )


def _repair_item_view(item: EditionRepairItem) -> dict[str, Any]:
    return {
        "repair_key": item.repair_key,
        "kind": item.kind,
        "position": item.position,
        "subject_id": item.subject_id,
        "article_title": item.article_title,
        "run_id": item.run_id,
        "pipeline_generation": item.pipeline_generation,
        "artifact_id": item.artifact_id,
        "artifact_version": item.artifact_version,
        "source_id": item.source_id,
        "source_title": item.source_title,
        "source_url": item.source_url,
        "collection_id": item.collection_id,
        "collection_state": item.collection_state,
        "artifact_type": item.artifact_type,
        "preview": item.preview,
        "reason_code": item.reason_code,
        "value_sha256": item.value_sha256,
        "payload_available": item.payload_available,
        "effective_action": item.effective_action,
        "effective_decision_id": item.effective_decision_id,
        "resolved": item.resolved,
        "resolution_reason": item.resolution_reason,
        "rebuild_required": item.rebuild_required,
        "recommended_stage": item.recommended_stage,
        "repair_state": item.repair_state,
        "is_publication_ioc": item.is_publication_ioc,
        "in_publication_scope": item.in_publication_scope,
        "application_state": item.application_state,
    }


def _production_repair_decision_view(decision: Any | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "id": str(decision.id),
        "action": decision.action.value,
        "actor_id": decision.actor_id,
        "reason": decision.reason,
        "created_at": decision.created_at.isoformat(),
        "observed_artifact_id": str(decision.observed_artifact_id),
        "observed_pipeline_generation": decision.observed_pipeline_generation,
    }


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
            detail={"code": str(exc)},
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

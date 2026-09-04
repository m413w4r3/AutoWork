from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import (
    DuplicateJobError,
    JobCancelledError,
    JobDispatcher,
    JobService,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_resolver import current_publication_artifact
from cti_app.application.production_batch_repointing import _repoint_batch_item
from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageChain,
    ProductionStageParameters,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_read_model import BatchStatusReadService
from cti_app.application.production_reconciliation import (
    ProductionReconciliationError,
    ProductionReconciliationService,
)
from cti_app.application.production_reconciliation_resolver import (
    ProductionReconciliationResolver,
    ReconciliationOutcome,
)
from cti_app.application.production_recovery import (
    ProductionRecoveryDisposition,
    ProductionRecoveryPolicyV1,
)
from cti_app.application.production_repairs import (
    ProductionReferenceRepairError,
    ProductionReferenceRepairService,
    ProductionRepairDecisionService,
    ProductionRepairIssueService,
    ProductionRepairProjectionError,
    ProductionRepairProjectionService,
    ProductionRepairResolvedError,
    ProductionRepairStaleError,
    ProductionRepairStatusError,
)
from cti_app.application.production_stage_status import (
    build_stage_statuses,
    completed_stage_count,
)
from cti_app.application.production_state import (
    ProductionStateError,
    ProductionStateImportResult,
    ProductionStateService,
    ProductionStateSnapshotV2,
)
from cti_app.application.subject_production import (
    EditionProductionBatchNotFoundError,
    EditionProductionBatchOwnershipError,
    EditionProductionService,
    ProductionRunNotFoundError,
    StaleEditionProductionBatchError,
    SubjectProductionService,
)
from cti_app.domain.editorial import EditorialGroup, EditorialGroupStatus
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ProductionArtifactStage,
    ProductionBatchCancellationConflictError,
    ProductionBatchStatus,
    ProductionReconciliationRequiredError,
    ProductionRepairAction,
    ProductionRepairIssueKind,
    ProductionReuseInvalidation,
    ProductionSubmissionReconciliation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.domain.publication import is_publication_ioc_artifact_type
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api", tags=["production"])

# Collection states that count as "available for analysis".
_ARCHIVED_STATES = {"archived", "extracted", "completed"}


class StartSubjectProductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartEditionProductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # subject_ids omitted -> every selected article of the edition is produced.
    subject_ids: list[UUID] | None = None


class RetryProductionStageRequest(BaseModel):
    stage: SubjectProductionStage


class ProductionReuseInvalidationRequest(BaseModel):
    from_stage: SubjectProductionStage


class ReconciliationAdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sha256: str = Field(..., min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


class ManualReconciliationRequest(ReconciliationAdoptRequest):
    markdown: str = Field(..., min_length=1, max_length=10_000_000)


class ManualReconciliationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(..., min_length=1, max_length=10_000_000)


class DeclareReconciliationLostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool
    reason: str = ""


class RebuildReferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume: bool = False


class SupplementalRepairDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["continue_without_source"]
    observed_artifact_id: UUID
    observed_pipeline_generation: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=500)


class ProductionRepairDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["include", "exclude", "continue_without_source"]
    observed_artifact_id: UUID
    observed_pipeline_generation: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=500)


class ApplyProductionRepairsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume: bool = False


class StageStatus(BaseModel):
    status: str  # pending, running, succeeded, needs_review, failed
    version: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    reused: bool = False
    reused_from_artifact_id: UUID | None = None
    reused_from_created_at: str | None = None
    research_date: str | None = None


class ExtractionRejections(BaseModel):
    q2_rejected_rules: list[dict[str, Any]] = Field(default_factory=list)
    q2_rejected_rule_count: int = 0
    q2_rejected_artifact_count: int = 0
    q2_rejected_ioc_count: int = 0
    q2_rejected_other_artifact_count: int = 0
    q2_source_evidence_rejections: list[dict[str, Any]] = Field(default_factory=list)


class ProductionStatus(BaseModel):
    subject_id: str
    edition_id: str
    title: str
    status: str  # queued, running, ready, needs_review, failed, cancelled
    current_stage: str
    progress_current: int
    progress_total: int
    references_conversation_id: str | None = None
    synthesis_conversation_id: str | None = None
    run_id: str
    pipeline_generation: int = 0
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    recovery_disposition: ProductionRecoveryDisposition
    extraction_progress: dict[str, Any] | None = None
    extraction_rejections: ExtractionRejections = Field(default_factory=ExtractionRejections)
    reconciliation: ProductionReconciliationView | None = None
    # Set when this run belongs to an edition production batch: such a run is
    # only ever resumed through the batch, never restarted standalone.
    batch_id: str | None = None
    # Parser recoveries worth showing to an analyst, never blocking.
    warnings: list[str] = []
    stages: dict[str, StageStatus]


class BatchItemDetail(BaseModel):
    # Lets the UI show "1/23" with names.
    position: int
    subject_id: str
    title: str
    run_id: str
    status: SubjectProductionStatus
    current_stage: SubjectProductionStage
    pipeline_generation: int
    auto_recovery_count: int
    error_code: str | None = None
    error_message: str | None = None
    extraction_progress: dict[str, Any] | None = None
    reconciliation: ProductionReconciliationView | None = None


class ProductionReconciliationView(BaseModel):
    production_run_id: str
    model_run_id: str
    bridge_response_id: str | None
    submission_state: str
    phase: str
    stage: SubjectProductionStage
    pipeline_generation: int
    output_sha256: str | None = None
    provenance: str | None = None
    visible_available: bool
    batch_id: str | None = None


ProductionStatus.model_rebuild()
BatchItemDetail.model_rebuild()


class BatchStatus(BaseModel):
    batch_id: str
    edition_id: str
    status: ProductionBatchStatus
    items: int
    completed: int
    needs_review: int
    failed: int
    cancelled: int = 0
    item_details: list[BatchItemDetail] = []
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    phase: str
    next_dispatch_at: str | None


def _runtime(request: Request) -> tuple[UnitOfWorkFactory, JobService, JobDispatcher]:
    return (
        request.app.state.uow_factory,
        request.app.state.job_service,
        request.app.state.job_dispatcher,
    )


async def _selected_article_group(request: Request, subject_id: UUID) -> EditorialGroup:
    async with request.app.state.uow_factory() as uow:
        group = cast(EditorialGroup | None, await uow.editorial_groups.get_by_subject(subject_id))
        if group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No editorial group found for subject {subject_id}",
            )
        if group.status != EditorialGroupStatus.SELECTED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Subject is not selected",
            )
        return group


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


def _production_state_error(exc: ProductionStateError) -> HTTPException:
    code_to_status = {
        "production_state_not_found": status.HTTP_404_NOT_FOUND,
        "production_state_active_run": status.HTTP_409_CONFLICT,
        "production_state_incomplete": status.HTTP_409_CONFLICT,
        "production_state_unverified": status.HTTP_409_CONFLICT,
        "production_state_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
        "production_state_invalid_format": status.HTTP_400_BAD_REQUEST,
        "production_state_version_unsupported": status.HTTP_400_BAD_REQUEST,
        "production_state_invalid": status.HTTP_400_BAD_REQUEST,
        "production_state_checksum_mismatch": status.HTTP_400_BAD_REQUEST,
        "production_state_research_date_required": status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(
        status_code=code_to_status[exc.code],
        detail={"code": exc.code, "message": exc.message},
    )


def _production_state_service(request: Request) -> ProductionStateService:
    artifact_store = getattr(request.app.state, "production_artifact_store", None)
    if artifact_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "production_state_storage_unavailable",
                "message": "Le stockage des artefacts de production est indisponible.",
            },
        )
    return ProductionStateService(request.app.state.uow_factory, artifact_store)


def _production_reconciliation_service(request: Request) -> ProductionReconciliationService:
    return ProductionReconciliationService(
        request.app.state.uow_factory,
        request.app.state.model_gateway,
        request.app.state.job_service,
        request.app.state.job_dispatcher,
        getattr(request.app.state, "bridge_capabilities_provider", None),
    )


def _production_reconciliation_resolver(request: Request) -> ProductionReconciliationResolver:
    return ProductionReconciliationResolver(
        request.app.state.uow_factory,
        transport=getattr(request.app.state, "bridge_capabilities_provider", None),
        model_gateway=getattr(request.app.state, "model_gateway", None),
        model_conversation_service=getattr(
            request.app.state, "model_conversation_service", None
        ),
        diagnostics=getattr(request.app.state, "production_diagnostics", None),
    )


def _production_repair_issue_service(request: Request) -> ProductionRepairIssueService:
    service = getattr(request.app.state, "production_repair_issue_service", None)
    if service is None:
        service = ProductionRepairIssueService(
            request.app.state.uow_factory,
            getattr(request.app.state, "production_artifact_store", None),
        )
    return service


def _production_reference_repair_service(
    request: Request,
) -> ProductionReferenceRepairService:
    service = getattr(request.app.state, "production_reference_repair_service", None)
    if service is None:
        service = ProductionReferenceRepairService(
            request.app.state.uow_factory,
            getattr(request.app.state, "production_artifact_store", None),
        )
    return service


def _production_repair_decision_service(request: Request) -> ProductionRepairDecisionService:
    service = getattr(request.app.state, "production_repair_decision_service", None)
    if service is None:
        service = ProductionRepairDecisionService(request.app.state.uow_factory)
    return service


def _production_repair_projection_service(
    request: Request,
) -> ProductionRepairProjectionService:
    service = getattr(request.app.state, "production_repair_projection_service", None)
    if service is None:
        service = ProductionRepairProjectionService(
            request.app.state.uow_factory,
            getattr(request.app.state, "production_artifact_store", None),
        )
    return service


def _repair_decision_view(decision: Any | None) -> dict[str, Any] | None:
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


def _supplemental_repair_issue_view(issue: Any) -> dict[str, Any]:
    return {
        "repair_key": issue.repair_key,
        "kind": issue.kind.value,
        "source_id": issue.source_id,
        "source_title": issue.source_title,
        "source_url": issue.source_url,
        "publisher": issue.publisher,
        "collection_id": str(issue.collection_id),
        "collection_state": issue.collection_state,
        "error_reason": issue.error_reason,
        "attempt_count": issue.attempt_count,
        "production_run_id": str(issue.production_run_id),
        "observed_artifact_id": str(issue.observed_artifact_id),
        "observed_artifact_version": issue.observed_artifact_version,
        "observed_pipeline_generation": issue.observed_pipeline_generation,
        "effective_decision": _repair_decision_view(issue.effective_decision),
        "recommended_action": issue.recommended_action,
    }


def _repair_issue_view(issue: Any) -> dict[str, Any]:
    return {
        "repair_key": issue.repair_key,
        "kind": issue.kind.value,
        "artifact_type": issue.artifact_type,
        "source_id": issue.source_id,
        "source_title": issue.source_title,
        "is_publication_ioc": issue.is_publication_ioc,
        "source_url": issue.source_url,
        "reason_code": issue.reason_code,
        "value_sha256": issue.value_sha256,
        "preview": issue.preview,
        "payload_available": issue.payload_available,
        "production_run_id": str(issue.production_run_id),
        "observed_artifact_id": str(issue.observed_artifact_id),
        "observed_artifact_version": issue.observed_artifact_version,
        "observed_pipeline_generation": issue.observed_pipeline_generation,
        "model_run_id": issue.model_run_id,
        "batch_id": issue.batch_id,
        "effective_decision": _repair_decision_view(issue.effective_decision),
    }


def _repair_cursor(offset: int) -> str:
    raw = str(offset).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _repair_cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        offset = int(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_repair_cursor"},
        ) from exc
    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_repair_cursor"},
        )
    return offset


async def _ensure_reconciliation_required(request: Request, run_id: UUID) -> None:
    async with request.app.state.uow_factory() as uow:
        run = await uow.subject_production_runs.get(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "production_run_not_found",
                "message": "Le run de production est introuvable.",
            },
        )
    if not run.requires_reconciliation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "production_reconciliation_not_required",
                "message": "Ce run n'attend pas de réconciliation.",
            },
        )


async def _probe_production_reconciliation(
    request: Request, run_id: UUID
) -> tuple[ProductionReconciliationResolver, dict[str, object]]:
    await _ensure_reconciliation_required(request, run_id)
    resolver = _production_reconciliation_resolver(request)
    outcome = await resolver.resolve(run_id)
    return resolver, {
        "outcome": outcome.value,
        "bridge_status": getattr(resolver, "_last_bridge_status", None),
    }


def _reconciliation_error(exc: ProductionReconciliationError) -> HTTPException:
    not_found = {
        "production_run_not_found",
        "production_reconciliation_model_run_missing",
        "production_reconciliation_batch_missing",
    }
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND
        if exc.code in not_found
        else status.HTTP_409_CONFLICT,
        detail={"code": exc.code, "message": exc.message},
    )


def _production_pacing(request: Request) -> ProductionPacingPolicy:
    return getattr(request.app.state, "production_pacing", ProductionPacingPolicy.zero())


def _eligible_article_subject_ids(groups: Iterable[EditorialGroup]) -> list[UUID]:
    """Subjects of an edition that are selected articles, in board order."""
    return [
        group.subject_id
        for group in groups
        if group.subject_id is not None and group.status == EditorialGroupStatus.SELECTED
    ]


def _collect_warnings(artifacts: Sequence[Any]) -> list[str]:
    """Parser warnings recorded by each stage, in stage order."""
    out: list[str] = []
    for artifact in artifacts:
        for warning in artifact.metadata.get("warnings", []):
            if warning not in out:
                out.append(str(warning))
    return out


def _rejection_entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], entry) for entry in value if isinstance(entry, dict)]


def _extraction_rejections(artifact: Any | None) -> ExtractionRejections:
    if artifact is None:
        return ExtractionRejections()

    metadata = getattr(artifact, "metadata", {})
    verification = (
        metadata.get("deterministic_verification", {})
        if isinstance(metadata, dict)
        else {}
    )
    if not isinstance(verification, dict):
        return ExtractionRejections()

    rejections = _rejection_entries(verification.get("q2_source_evidence_rejections"))
    rules = _rejection_entries(verification.get("q2_rejected_rules"))
    if not rules:
        rules = [entry for entry in rejections if entry.get("proposal_kind") == "rule"]

    rule_count = verification.get("q2_rejected_rule_count")
    if not isinstance(rule_count, int):
        rule_count = len(rules)
    artifact_count = verification.get("q2_rejected_artifact_count")
    if not isinstance(artifact_count, int):
        artifact_count = len(rejections) - len(rules)
    ioc_count = verification.get("q2_rejected_ioc_count")
    if not isinstance(ioc_count, int):
        ioc_count = sum(
            is_publication_ioc_artifact_type(entry.get("artifact_type"))
            for entry in rejections
            if entry.get("proposal_kind") == "artifact"
        )
    other_artifact_count = verification.get("q2_rejected_other_artifact_count")
    if not isinstance(other_artifact_count, int):
        other_artifact_count = max(0, artifact_count - ioc_count)

    return ExtractionRejections(
        q2_rejected_rules=rules,
        q2_rejected_rule_count=rule_count,
        q2_rejected_artifact_count=artifact_count,
        q2_rejected_ioc_count=ioc_count,
        q2_rejected_other_artifact_count=other_artifact_count,
        q2_source_evidence_rejections=rejections[:200],
    )


def _run_view(
    run: SubjectProductionRun, edition_id: UUID, *, job_id: UUID | None
) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "subject_id": str(run.subject_id),
        "edition_id": str(edition_id),
        "status": run.status.value,
        "stage": run.current_stage.value,
        "job_id": str(job_id) if job_id else None,
        "created_at": run.created_at.isoformat(),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "error_details": run.error_details,
    }


def reconciliation_view(
    run_id: UUID,
    reconciliation: ProductionSubmissionReconciliation | None,
    *,
    pipeline_generation: int,
    batch_id: UUID | None = None,
) -> ProductionReconciliationView | None:
    if reconciliation is None:
        return None
    return ProductionReconciliationView(
        production_run_id=str(run_id),
        model_run_id=str(reconciliation.model_run_id),
        bridge_response_id=reconciliation.bridge_response_id,
        submission_state=reconciliation.submission_state.value,
        phase=reconciliation.phase,
        stage=reconciliation.stage,
        pipeline_generation=pipeline_generation,
        output_sha256=reconciliation.output_sha256,
        provenance=reconciliation.provenance,
        visible_available=reconciliation.bridge_response_id is not None,
        batch_id=str(batch_id) if batch_id else None,
    )


async def _batch_status_view(uow: Any, batch: Any) -> BatchStatus:
    """Build a batch response from the UI-optimized read model."""
    items = await BatchStatusReadService(uow.batch_status_read_model).list_items(batch.id)
    completed = needs_review = failed = cancelled = 0
    details: list[BatchItemDetail] = []
    for item in items:
        if item.status is SubjectProductionStatus.READY:
            completed += 1
        elif item.status is SubjectProductionStatus.NEEDS_REVIEW:
            needs_review += 1
        elif item.status is SubjectProductionStatus.FAILED:
            failed += 1
        elif item.status is SubjectProductionStatus.CANCELLED:
            cancelled += 1

        details.append(
            BatchItemDetail(
                position=item.position,
                subject_id=str(item.subject_id),
                title=item.title,
                run_id=str(item.run_id),
                status=item.status,
                current_stage=item.current_stage,
                pipeline_generation=item.pipeline_generation,
                auto_recovery_count=item.auto_recovery_count,
                error_code=item.error_code,
                error_message=item.error_message,
                extraction_progress=item.extraction_progress,
                reconciliation=(
                    reconciliation_view(
                        item.run_id,
                        item.reconciliation
                        if item.status is SubjectProductionStatus.NEEDS_REVIEW
                        and item.error_code == PRODUCTION_RECONCILIATION_ERROR_CODE
                        else None,
                        pipeline_generation=item.pipeline_generation,
                        batch_id=batch.id,
                    )
                ),
            )
        )
    return BatchStatus(
        batch_id=str(batch.id),
        edition_id=str(batch.edition_id),
        status=batch.status,
        items=len(details),
        completed=completed,
        needs_review=needs_review,
        failed=failed,
        cancelled=cancelled,
        item_details=details,
        created_at=batch.created_at.isoformat(),
        started_at=batch.started_at.isoformat() if batch.started_at else None,
        finished_at=batch.finished_at.isoformat() if batch.finished_at else None,
        phase=batch.phase.value,
        next_dispatch_at=(batch.next_dispatch_at.isoformat() if batch.next_dispatch_at else None),
    )


async def _start_production_run(
    uow_factory: UnitOfWorkFactory,
    jobs: JobService,
    dispatcher: JobDispatcher,
    *,
    run: SubjectProductionRun,
    actor_id: str,
) -> tuple[SubjectProductionRun, UUID | None]:
    """Start one queued run and submit exactly its SOURCES job."""
    service = SubjectProductionService(uow_factory)
    if run.status is SubjectProductionStatus.RUNNING:
        return run, None
    if run.status is not SubjectProductionStatus.QUEUED:
        return run, None

    # start_run persists and returns the RUNNING run; keep that object, not
    # the stale QUEUED one returned by create_run.
    run = await service.start_run(run.id)

    if not await _production_run_can_dispatch(uow_factory, run.id):
        return run, None

    # The idempotency key makes a concurrent duplicate POST reuse this job.
    parameters = ProductionStageParameters(
        run_id=run.id,
        expected_stage=SubjectProductionStage.SOURCES.value,
        pipeline_generation=run.pipeline_generation,
    )
    try:
        job = await jobs.submit(
            kind="production.subject.sources",
            aggregate_type="subject",
            aggregate_id=run.subject_id,
            idempotency_key=production_stage_idempotency_key(run, SubjectProductionStage.SOURCES),
            correlation_id=get_correlation_id(),
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
            actor_id=actor_id,
        )
    except DuplicateJobError as exc:
        return run, exc.existing_job_id
    if not await _production_run_can_dispatch(uow_factory, run.id):
        await _cancel_non_terminal_run_jobs(
            jobs,
            [(run.id, run.subject_id)],
            actor_id=actor_id,
        )
        return run, job.id
    await dispatcher.dispatch(job.id)
    return run, job.id


async def _create_and_start_run(
    uow_factory: UnitOfWorkFactory,
    jobs: JobService,
    dispatcher: JobDispatcher,
    *,
    subject_id: UUID,
    edition_id: UUID,
    actor_id: str,
) -> tuple[SubjectProductionRun, UUID | None]:
    """Creates (or reuses an in-flight) run and submits its SOURCES job.

    Shared by "start production" and "retry references": both must go through
    `SubjectProductionService.create_run`'s idempotency so a duplicate POST
    never creates a second run nor submits a second job.
    """
    service = SubjectProductionService(uow_factory)
    run, created = await service.create_run(
        subject_id=subject_id,
        edition_id=edition_id,
    )

    del created
    return await _start_production_run(
        uow_factory,
        jobs,
        dispatcher,
        run=run,
        actor_id=actor_id,
    )


async def _production_run_can_dispatch(
    uow_factory: UnitOfWorkFactory,
    run_id: UUID,
    *,
    batch_id: UUID | None = None,
) -> bool:
    """Re-read the exact run (and batch, when applicable) before dispatch."""
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get(run_id)
        if run is None or run.status is not SubjectProductionStatus.RUNNING:
            return False
        if batch_id is None:
            return True
        batch = await uow.edition_production_batches.get(batch_id)
        return batch is not None and batch.status in {
            ProductionBatchStatus.QUEUED,
            ProductionBatchStatus.RUNNING,
        }


async def _dispatch_handed_off_production_run(
    request: Request,
    started: SubjectProductionRun,
    batch_id: UUID,
    *,
    actor_id: str,
) -> None:
    """Submit a hand-off stage only after revalidating the exact run/batch."""
    uow_factory, jobs, dispatcher = _runtime(request)
    async with uow_factory() as uow:
        latest = await uow.subject_production_runs.get(started.id)
    if latest is None or latest.status is SubjectProductionStatus.CANCELLED:
        return
    if not await _production_run_can_dispatch(uow_factory, latest.id, batch_id=batch_id):
        return

    pacing = _production_pacing(request)
    batch_service = EditionProductionService(uow_factory, pacing)
    delay_ms = await batch_service.next_dispatch_delay_ms(batch_id)
    if latest.current_stage in {
        SubjectProductionStage.REFERENCES,
        SubjectProductionStage.SYNTHESIS,
    }:
        delay_ms += pacing.model_delay_ms(latest.current_stage)

    chain = ProductionStageChain(pacing)
    chain.bind(jobs, dispatcher)
    try:
        await chain.submit(
            run=latest,
            stage=latest.current_stage,
            correlation_id=get_correlation_id(),
            actor_id=actor_id,
            delay_ms=delay_ms,
            before_dispatch=lambda: _production_run_can_dispatch(
                uow_factory, latest.id, batch_id=batch_id
            ),
        )
    except (DuplicateJobError, JobCancelledError):
        # Another worker may have completed this hand-off, or cancellation may
        # have won the final fence.  Both outcomes are already persisted.
        return


async def _retry_production_run(
    request: Request,
    run_id: UUID,
    payload: RetryProductionStageRequest,
    actor_id: str,
) -> dict[str, Any]:
    """Retry exactly one persisted production run and enqueue its stage."""
    uow_factory, jobs, dispatcher = _runtime(request)

    service = SubjectProductionService(uow_factory)
    try:
        retry = await service.retry_from_stage(run_id, payload.stage)
    except ProductionRunNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No production run found for run {run_id}",
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": str(e)},
        ) from e
    run = retry.run
    old_generation = retry.old_generation
    staled = retry.staled_artifacts

    parameters = ProductionStageParameters(
        run_id=run.id,
        expected_stage=payload.stage.value,
        pipeline_generation=run.pipeline_generation,
    )
    job = await jobs.submit(
        kind=stage_job_kind(payload.stage),
        aggregate_type="subject",
        aggregate_id=run.subject_id,
        idempotency_key=production_stage_idempotency_key(run, payload.stage),
        correlation_id=get_correlation_id(),
        input_parameters=parameters.model_dump(mode="json"),
        max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
        actor_id=actor_id,
    )
    await dispatcher.dispatch(
        job.id,
        delay_ms=_production_pacing(request).model_delay_ms(payload.stage),
    )
    diagnostics = getattr(request.app.state, "production_diagnostics", None)
    if diagnostics is not None:
        diagnostics.record(
            event="production.stage_retry_requested",
            run_id=run.id,
            subject_id=run.subject_id,
            stage=payload.stage.value,
            correlation_id=get_correlation_id(),
            requested_stage=payload.stage.value,
            previous_status=retry.previous_status.value,
            previous_stage=retry.previous_stage.value,
            old_generation=old_generation,
            new_generation=run.pipeline_generation,
            staled_artifacts=staled,
            job_id=str(job.id),
            actor_id=actor_id,
        )

    view = _run_view(run, run.edition_id, job_id=job.id)
    view["action"] = "stage_retry_requested"
    view["requested_stage"] = payload.stage.value
    view["old_generation"] = old_generation
    view["pipeline_generation"] = run.pipeline_generation
    view["staled_artifacts"] = staled
    return view


async def _cancel_non_terminal_run_jobs(
    jobs: JobService,
    runs: Sequence[tuple[UUID, UUID]],
    *,
    actor_id: str,
) -> None:
    """Request cancellation only for jobs carrying an exact production run ID."""
    list_jobs = getattr(jobs, "list_for_aggregate", None)
    cancel_job = getattr(jobs, "cancel", None)
    if list_jobs is None or cancel_job is None:
        return
    for run_id, subject_id in runs:
        for job in await list_jobs("subject", subject_id):
            if str(job.input_parameters.get("run_id")) == str(run_id) and not job.is_terminal:
                try:
                    await cancel_job(job.id, actor_id=actor_id)
                except (LookupError, ValueError):
                    pass


async def _cancel_production_run(
    request: Request,
    run_id: UUID,
    *,
    actor_id: str,
) -> dict[str, Any]:
    """Cancel one exact run, then request cancellation of its exact jobs."""
    uow_factory, jobs, _ = _runtime(request)
    service = SubjectProductionService(uow_factory)
    try:
        cancellation = await service.cancel_run_with_result(run_id)
    except ProductionRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No production run found for run {run_id}",
        ) from exc
    except ValueError as exc:
        if str(exc) != "production_run_not_cancellable":
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "production_run_not_cancellable",
                "message": "Cette tentative de production n'est plus active.",
            },
        ) from exc

    await _cancel_non_terminal_run_jobs(
        jobs,
        [(cancellation.run.id, cancellation.run.subject_id)],
        actor_id=actor_id,
    )
    if cancellation.changed and cancellation.batch_id is not None:
        batch_service = EditionProductionService(uow_factory, _production_pacing(request))
        started = await batch_service.on_subject_terminal(
            cancellation.batch_id,
            cancellation.run.id,
            actor_id=actor_id,
            correlation_id=get_correlation_id(),
        )
        if started is not None:
            await _dispatch_handed_off_production_run(
                request,
                started,
                cancellation.batch_id,
                actor_id=actor_id,
            )
    return {
        "action": "cancel",
        "run_id": str(cancellation.run.id),
        "status": SubjectProductionStatus.CANCELLED.value,
    }


# Subject Production Endpoints


@router.post("/subjects/{subject_id}/production")
async def start_subject_production(
    subject_id: UUID,
    request: Request,
    body: StartSubjectProductionRequest | None = None,
) -> dict[str, Any]:
    uow_factory, jobs, dispatcher = _runtime(request)

    async with uow_factory() as uow:
        group = await uow.editorial_groups.get_by_subject(subject_id)
        if group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No editorial group found for subject {subject_id}",
            )
        if group.status != EditorialGroupStatus.SELECTED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Subject is not selected",
            )
        edition_id = group.edition_id

        # A subject produced inside an edition batch is repaired through that
        # batch, never by a standalone run: once the current run is terminal,
        # starting again would create a run the batch does not own — invisible
        # to Review and unable to repair the batch item it appears to replace.
        current = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if current is not None and current.status not in {
            SubjectProductionStatus.QUEUED,
            SubjectProductionStatus.RUNNING,
        }:
            get_by_run = getattr(uow.edition_production_batch_items, "get_by_run", None)
            item = await get_by_run(current.id) if get_by_run is not None else None
            if item is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "production_run_batch_owned"},
                )

    try:
        run, job_id = await _create_and_start_run(
            uow_factory,
            jobs,
            dispatcher,
            subject_id=subject_id,
            edition_id=edition_id,
            actor_id=await _actor_id(request),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    return _run_view(run, edition_id, job_id=job_id)


@router.post("/production/subjects/{subject_id}/production/restart-with-new-sources")
async def restart_subject_with_new_sources(
    subject_id: UUID,
    request: Request,
) -> dict[str, str]:
    """Create a fresh-input run after an analyst replaced a discovery URL."""
    uow_factory, jobs, dispatcher = _runtime(request)
    async with uow_factory() as uow:
        current = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )
        if current.status in {
            SubjectProductionStatus.QUEUED,
            SubjectProductionStatus.RUNNING,
        }:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "production_run_active",
                    "message": "Cette tentative de production est encore en cours.",
                },
            )
        replaced_run_id = current.id
        edition_id = current.edition_id

    actor_id = await _actor_id(request)
    service = SubjectProductionService(uow_factory)
    try:
        run, created = await service.create_run(
            subject_id=subject_id,
            edition_id=edition_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # A concurrent restart may have won after the initial read; never attach a
    # second request to an active run or submit its SOURCES job twice.
    if not created and run.status in {
        SubjectProductionStatus.QUEUED,
        SubjectProductionStatus.RUNNING,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "production_run_active",
                "message": "Cette tentative de production est encore en cours.",
            },
        )

    async with uow_factory() as uow:
        await _repoint_batch_item(uow, replaced_run_id, run.id)
        await uow.commit()

    await _start_production_run(
        uow_factory,
        jobs,
        dispatcher,
        run=run,
        actor_id=actor_id,
    )
    return {"run_id": str(run.id), "replaced_run_id": str(replaced_run_id)}


@router.get("/subjects/{subject_id}/production/state/export")
async def export_subject_production_state(
    subject_id: UUID,
    request: Request,
) -> ProductionStateSnapshotV2:
    group = await _selected_article_group(request, subject_id)
    service = _production_state_service(request)
    try:
        return await service.export_state(subject_id=subject_id, subject_title=group.title)
    except ProductionStateError as exc:
        raise _production_state_error(exc) from exc


@router.post("/subjects/{subject_id}/production/state/import")
async def import_subject_production_state(
    subject_id: UUID,
    request: Request,
    payload: dict[str, Any],
) -> ProductionStateImportResult:
    group = await _selected_article_group(request, subject_id)
    service = _production_state_service(request)
    try:
        return await service.import_state(
            subject_id=subject_id,
            edition_id=group.edition_id,
            payload=payload,
        )
    except ProductionStateError as exc:
        raise _production_state_error(exc) from exc


@router.get("/subjects/{subject_id}/production")
async def get_subject_production(
    subject_id: UUID,
    request: Request,
) -> ProductionStatus:
    # 404 here is the signal the UI uses to offer "start production".
    uow_factory, _, _ = _runtime(request)

    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )

        group = await uow.editorial_groups.get_by_subject(subject_id)
        get_by_run = getattr(uow.edition_production_batch_items, "get_by_run", None)
        batch_item = await get_by_run(run.id) if get_by_run is not None else None
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "production_input_snapshot_missing"},
            )

        # Artifacts evidence the stages that produce one; SOURCES does not.
        artifacts = await uow.production_artifacts.list_for_run(run.id)
        extraction_artifact = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.EXTRACTION.value
        )
        artifacts_by_stage = {a.stage.value: a for a in artifacts}
        collections = await uow.source_collections.list_for_subject(subject_id)
        archived_sources = sum(1 for c in collections if c.state in _ARCHIVED_STATES)

        stages = build_stage_statuses(
            run,
            artifacts_by_stage,
            archived_sources=archived_sources,
            research_date=snapshot.research_date,
        )
        completed_stages = completed_stage_count(stages)

        return ProductionStatus(
            subject_id=str(run.subject_id),
            edition_id=str(run.edition_id),
            title=(
                snapshot.subject_title
                if snapshot.subject_title
                else group.title
                if group
                else str(run.subject_id)
            ),
            status=run.status.value,
            current_stage=run.current_stage.value,
            progress_current=completed_stages,
            progress_total=len(stages),
            references_conversation_id=(
                str(run.references_conversation_id) if run.references_conversation_id else None
            ),
            synthesis_conversation_id=(
                str(run.synthesis_conversation_id) if run.synthesis_conversation_id else None
            ),
            run_id=str(run.id),
            pipeline_generation=run.pipeline_generation,
            created_at=run.created_at.isoformat(),
            started_at=run.started_at.isoformat() if run.started_at else None,
            finished_at=run.finished_at.isoformat() if run.finished_at else None,
            error_code=run.error_code,
            error_message=run.error_message,
            error_details=run.error_details,
            recovery_disposition=ProductionRecoveryPolicyV1.disposition_for_run(run),
            extraction_progress=run.extraction_progress,
            extraction_rejections=_extraction_rejections(extraction_artifact),
            reconciliation=(
                reconciliation_view(
                    run.id,
                    run.reconciliation
                    if run.status is SubjectProductionStatus.NEEDS_REVIEW
                    and run.error_code == PRODUCTION_RECONCILIATION_ERROR_CODE
                    else None,
                    pipeline_generation=run.pipeline_generation,
                )
            ),
            batch_id=str(batch_item.batch_id) if batch_item is not None else None,
            warnings=_collect_warnings(artifacts),
            stages={name: StageStatus(**stage) for name, stage in stages.items()},
        )


@router.get("/subjects/{subject_id}/investigation")
async def get_subject_investigation(subject_id: UUID, request: Request) -> dict[str, Any]:
    """Read-only visibility into the manual major-assisted checkpoint."""
    uow_factory, _, _ = _runtime(request)
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No production run found"
            )
        investigation = await uow.analyst_investigations.get_for_run(run.id)
        if investigation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No investigation found"
            )
        result: dict[str, Any] = {
            "investigation_id": str(investigation.id),
            "production_run_id": str(investigation.production_run_id),
            "status": investigation.status.value,
            "stage": investigation.current_stage.value,
            "cycle_number": investigation.cycle_number,
            "synthesis_artifact_id": str(investigation.synthesis_artifact_id),
            "input_pack_blob_id": str(investigation.input_pack_blob_id)
            if investigation.input_pack_blob_id
            else None,
            "input_sha256": investigation.input_sha256,
            "file_indicators": None,
        }
        store = getattr(request.app.state, "production_artifact_store", None)
        if store is not None and investigation.input_pack_blob_id is not None:
            result["file_indicators"] = (
                await store.read_json(investigation.input_pack_blob_id)
            ).get("file_indicators", [])
        return result


@router.get("/subjects/{subject_id}/production/repairs")
async def get_subject_production_repairs(
    subject_id: UUID,
    request: Request,
    kind: ProductionRepairIssueKind | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, Any]:
    """List current Repair Desk issues with cursor pagination."""
    uow_factory = request.app.state.uow_factory
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )
        edition_id = run.edition_id

    issue_service = _production_repair_issue_service(request)
    supplemental = await issue_service.list_supplemental_source_issues(
        edition_id, subject_id
    )
    light_getter = getattr(issue_service, "list_issue_views", None)
    extraction_issues = (
        await light_getter(edition_id, subject_id)
        if callable(light_getter)
        else await issue_service.list_issues(edition_id, subject_id)
    )
    if kind is ProductionRepairIssueKind.REJECTED_INDICATOR:
        supplemental = ()
        extraction_issues = tuple(
            issue for issue in extraction_issues if issue.kind is kind
        )
    elif kind is ProductionRepairIssueKind.REJECTED_RULE:
        supplemental = ()
        extraction_issues = tuple(issue for issue in extraction_issues if issue.kind is kind)
    elif kind is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED:
        extraction_issues = ()

    all_issues = [
        *[_repair_issue_view(issue) for issue in extraction_issues],
        *[_supplemental_repair_issue_view(issue) for issue in supplemental],
    ]
    all_issues.sort(key=lambda item: (item.get("kind", ""), item.get("repair_key", "")))
    offset = _repair_cursor_offset(cursor)
    page = all_issues[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = _repair_cursor(next_offset) if next_offset < len(all_issues) else None
    return {
        "subject_id": str(subject_id),
        "edition_id": str(edition_id),
        "production_run_id": str(run.id),
        "issues": page,
        "supplemental_source_issues": [
            item
            for item in page
            if item.get("kind")
            == ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED.value
        ],
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
    }


@router.get("/subjects/{subject_id}/production/repairs/{repair_key}")
async def get_subject_production_repair_detail(
    subject_id: UUID, repair_key: str, request: Request
) -> dict[str, Any]:
    """Return the complete inert value for one rejected Q2 object."""
    uow_factory = request.app.state.uow_factory
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )
        edition_id = run.edition_id
    detail = await _production_repair_issue_service(request).get_issue(
        edition_id, repair_key, subject_id
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "production_repair_issue_not_found"},
        )
    result = _repair_issue_view(detail.issue)
    result["value"] = detail.value
    result["body"] = (
        detail.value
        if detail.issue.kind is ProductionRepairIssueKind.REJECTED_RULE
        else None
    )
    result["provenance_model_run_id"] = detail.issue.model_run_id
    result["effective_decision"] = _repair_decision_view(detail.issue.effective_decision)
    return result


@router.post("/subjects/{subject_id}/production/repairs/rebuild-references")
async def rebuild_subject_references(
    subject_id: UUID,
    request: Request,
    payload: RebuildReferencesRequest | None = None,
) -> dict[str, Any]:
    """Rebuild Q1's canonical report from its already archived raw answer."""
    uow_factory = request.app.state.uow_factory
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )
        run_id = run.id

    try:
        result = await _production_reference_repair_service(request).rebuild_from_archived_q1(
            run_id,
            actor_id=await _actor_id(request),
        )
    except ProductionReconciliationRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "production_reconciliation_required",
                "message": "La réconciliation ChatGPT doit être résolue avant réparation.",
            },
        ) from exc
    except ProductionReferenceRepairError as exc:
        not_found = {"production_run_not_found", "references_artifact_not_found"}
        code = exc.code
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND if code in not_found else status.HTTP_409_CONFLICT
            ),
            detail={"code": code, "message": str(exc)},
        ) from exc

    response: dict[str, Any] = {
        "references_artifact_id": str(result.artifact.id),
        "changed": result.changed,
        "restored_source_count": len(result.restored_source_ids),
        "restored_event_count": len(result.restored_event_ids),
        "recommended_retry_stage": SubjectProductionStage.EXTRACTION.value,
    }
    if (payload or RebuildReferencesRequest()).resume:
        try:
            response["resume"] = await _retry_production_run(
                request,
                run_id,
                RetryProductionStageRequest(stage=SubjectProductionStage.EXTRACTION),
                await _actor_id(request),
            )
        except Exception as exc:
            # The rebuild transaction has already committed.  A queue or
            # dispatcher failure is reported without turning durable repair
            # state back into an apparent upload failure.
            response["resume_error"] = {
                "code": str(getattr(exc, "code", None) or type(exc).__name__),
                "message": str(exc),
            }
    return response


@router.post(
    "/subjects/{subject_id}/production/repairs/{repair_key}/decision"
)
async def decide_subject_production_repair(
    subject_id: UUID,
    repair_key: str,
    payload: ProductionRepairDecisionRequest,
    request: Request,
) -> dict[str, Any]:
    """Append a new decision for a rejected Q2 object or Q1 source issue."""
    uow_factory, _, _ = _runtime(request)
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No production run found",
            )
        edition_id = run.edition_id
    issue_service = _production_repair_issue_service(request)
    issue: Any
    expected_kind: ProductionRepairIssueKind | None
    if payload.action == ProductionRepairAction.CONTINUE_WITHOUT_SOURCE.value:
        issue = await issue_service.get_supplemental_source_issue(
            edition_id, repair_key, subject_id
        )
        expected_kind = ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED
    else:
        issue = await issue_service.get_issue(edition_id, repair_key, subject_id)
        expected_kind = issue.issue.kind if issue is not None else None
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "production_repair_issue_not_found"},
        )
    if payload.action == ProductionRepairAction.CONTINUE_WITHOUT_SOURCE.value:
        issue_artifact_id = issue.observed_artifact_id
        issue_generation = issue.observed_pipeline_generation
    else:
        issue_artifact_id = issue.issue.observed_artifact_id
        issue_generation = issue.issue.observed_pipeline_generation
    if (
        issue_artifact_id != payload.observed_artifact_id
        or issue_generation != payload.observed_pipeline_generation
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "production_repair_stale"},
        )
    if expected_kind is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "production_repair_issue_kind_missing"},
        )
    if payload.action == ProductionRepairAction.CONTINUE_WITHOUT_SOURCE.value:
        production_run_id = issue.production_run_id
    else:
        production_run_id = issue.issue.production_run_id

    try:
        decision = await _production_repair_decision_service(request).decide(
            edition_id=edition_id,
            subject_id=subject_id,
            production_run_id=production_run_id,
            observed_artifact_id=payload.observed_artifact_id,
            observed_pipeline_generation=payload.observed_pipeline_generation,
            repair_key=repair_key,
            issue_kind=expected_kind,
            action=ProductionRepairAction(payload.action),
            actor_id=await _actor_id(request),
            reason=payload.reason,
        )
    except ProductionRepairStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": ProductionRepairStaleError.code},
        ) from exc
    except ProductionRepairResolvedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": ProductionRepairResolvedError.code},
        ) from exc
    except ProductionRepairStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": str(exc)},
        ) from exc

    return {
        "repair_key": repair_key,
        "action": decision.action.value,
        "decision_id": str(decision.id),
        "resolved": True,
        "archive_created": False,
        "retry_required": expected_kind
        in {
            ProductionRepairIssueKind.REJECTED_INDICATOR,
            ProductionRepairIssueKind.REJECTED_RULE,
        },
    }


@router.post("/subjects/{subject_id}/production/repairs/apply")
async def apply_subject_production_repairs(
    subject_id: UUID,
    request: Request,
    payload: ApplyProductionRepairsRequest | None = None,
) -> dict[str, Any]:
    """Persist the effective extraction, then optionally resume SYNTHESIS."""
    uow_factory = request.app.state.uow_factory
    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )
        run_id = run.id

    try:
        result = await _production_repair_projection_service(request).project_effective_extraction(
            run_id,
            actor_id=await _actor_id(request),
        )
    except ProductionReconciliationRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "production_reconciliation_required"},
        ) from exc
    except ProductionRepairProjectionError as exc:
        code = str(exc)
        not_found = {"production_run_not_found", "extraction_artifact_not_found"}
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if code in not_found
                else status.HTTP_409_CONFLICT
            ),
            detail={"code": code, "message": code},
        ) from exc

    response: dict[str, Any] = {
        "changed": result.changed,
        "extraction_artifact_id": str(result.artifact.id),
        "accepted_indicator_count": result.accepted_indicator_count,
        "accepted_rule_count": result.accepted_rule_count,
        "unresolved_count": result.unresolved_count,
        "recommended_retry_stage": SubjectProductionStage.SYNTHESIS.value,
        "resumed": False,
    }
    if (payload or ApplyProductionRepairsRequest()).resume:
        try:
            await _retry_production_run(
                request,
                run_id,
                RetryProductionStageRequest(stage=SubjectProductionStage.SYNTHESIS),
                await _actor_id(request),
            )
            response["resumed"] = True
        except Exception as exc:
            # Projection persistence is committed independently. A dispatcher
            # failure must not make the analyst believe the projection vanished.
            response["resume_error"] = {
                "code": str(getattr(exc, "code", None) or type(exc).__name__),
                "message": str(exc),
            }
    return response


@router.post("/subjects/{subject_id}/production/retry")
async def retry_production_stage(
    subject_id: UUID,
    payload: RetryProductionStageRequest,
    request: Request,
) -> dict[str, Any]:
    """Deliberately recompute a stage in the current run and chain onward."""
    uow_factory, _, _ = _runtime(request)
    async with uow_factory() as uow:
        current = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not current:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )
        current_run_id = current.id

    return await _retry_production_run(request, current_run_id, payload, await _actor_id(request))


@router.post("/production/runs/{run_id}/reconciliation/visible/preview")
async def preview_visible_production_reconciliation(
    run_id: UUID,
    request: Request,
) -> dict[str, Any]:
    try:
        preview = await _production_reconciliation_service(request).preview_visible(run_id)
    except ProductionReconciliationError as exc:
        raise _reconciliation_error(exc) from exc
    return preview.as_dict()


@router.post("/production/runs/{run_id}/reconciliation/probe")
async def probe_production_reconciliation(
    run_id: UUID,
    request: Request,
) -> dict[str, object]:
    _, result = await _probe_production_reconciliation(request, run_id)
    return result


@router.post("/production/runs/{run_id}/reconciliation/declare-lost")
async def declare_lost_production_reconciliation(
    run_id: UUID,
    payload: DeclareReconciliationLostRequest,
    request: Request,
) -> dict[str, object]:
    if payload.confirm is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "production_reconciliation_confirmation_required",
                "message": "La confirmation explicite est requise.",
            },
        )

    resolver, probe = await _probe_production_reconciliation(request, run_id)
    if probe["outcome"] == ReconciliationOutcome.RESUMED.value:
        # A response was found: never discard it in favor of a new submission.
        return probe
    if probe["outcome"] == ReconciliationOutcome.RELEASED.value:
        # The probe already made the terminal negative decision.
        return probe

    outcome = await resolver.release_declared_lost(
        run_id,
        payload.reason,
        actor_id=await _actor_id(request),
    )
    if outcome is not ReconciliationOutcome.RELEASED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "production_reconciliation_not_required",
                "message": "Ce run n'attend plus de réconciliation.",
            },
        )
    return {"outcome": ReconciliationOutcome.RELEASED.value, "declared_lost": True}


@router.post("/production/runs/{run_id}/reconciliation/visible/adopt")
async def adopt_visible_production_reconciliation(
    run_id: UUID,
    payload: ReconciliationAdoptRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return await _production_reconciliation_service(request).adopt_visible(
            run_id, payload.expected_sha256, actor_id=await _actor_id(request)
        )
    except ProductionReconciliationError as exc:
        raise _reconciliation_error(exc) from exc


@router.post("/production/runs/{run_id}/reconciliation/manual/preview")
async def preview_manual_production_reconciliation(
    run_id: UUID,
    payload: ManualReconciliationPreviewRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        preview = await _production_reconciliation_service(request).preview_manual(
            run_id, payload.markdown
        )
    except ProductionReconciliationError as exc:
        raise _reconciliation_error(exc) from exc
    return preview.as_dict()


@router.post("/production/runs/{run_id}/reconciliation/manual/adopt")
async def adopt_manual_production_reconciliation(
    run_id: UUID,
    payload: ManualReconciliationRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return await _production_reconciliation_service(request).adopt_manual(
            run_id,
            payload.markdown,
            payload.expected_sha256,
            actor_id=await _actor_id(request),
        )
    except ProductionReconciliationError as exc:
        raise _reconciliation_error(exc) from exc


@router.post("/production/runs/{run_id}/reconciliation/visible/abandon")
async def abandon_visible_production_reconciliation(
    run_id: UUID,
    request: Request,
) -> dict[str, Any]:
    try:
        return await _production_reconciliation_service(request).abandon_visible(run_id)
    except ProductionReconciliationError as exc:
        raise _reconciliation_error(exc) from exc


@router.post("/subjects/{subject_id}/production/reuse/invalidate")
async def invalidate_production_reuse(
    subject_id: UUID,
    payload: ProductionReuseInvalidationRequest,
    request: Request,
) -> dict[str, Any]:
    """Prevent future cross-run reuse from one costly stage onward."""
    if payload.from_stage not in {
        SubjectProductionStage.REFERENCES,
        SubjectProductionStage.EXTRACTION,
        SubjectProductionStage.SYNTHESIS,
    }:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "reuse_invalidation_stage_not_allowed"},
        )

    actor_id = await _actor_id(request)
    uow_factory, _, _ = _runtime(request)
    async with uow_factory() as uow:
        lock_creation = getattr(uow.subject_production_runs, "lock_creation_for_subject", None)
        if lock_creation is not None:
            await lock_creation(subject_id)
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if run is None:
            raise HTTPException(status_code=404, detail="No production run found")
        if run.status in {SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "reuse_invalidation_run_active"},
            )
        occurred_at = datetime.now(UTC)
        invalidation = ProductionReuseInvalidation(
            edition_id=run.edition_id,
            subject_id=subject_id,
            from_stage=payload.from_stage,
            actor_id=actor_id,
            correlation_id=get_correlation_id(),
            occurred_at=occurred_at,
        )
        await uow.production_reuse_invalidations.add(invalidation)
        await uow.commit()

    return {
        "action": "production_reuse_invalidated",
        "subject_id": str(subject_id),
        "edition_id": str(run.edition_id),
        "from_stage": payload.from_stage.value,
        "occurred_at": occurred_at.isoformat(),
    }


@router.post("/production/runs/{run_id}/retry")
async def retry_production_run(
    run_id: UUID,
    payload: RetryProductionStageRequest,
    request: Request,
) -> dict[str, Any]:
    return await _retry_production_run(request, run_id, payload, await _actor_id(request))


@router.post("/production/runs/{run_id}/cancel")
async def cancel_production_run(
    run_id: UUID,
    request: Request,
) -> dict[str, Any]:
    return await _cancel_production_run(request, run_id, actor_id=await _actor_id(request))


@router.post("/subjects/{subject_id}/production/cancel")
async def cancel_production(
    subject_id: UUID,
    request: Request,
) -> dict[str, Any]:
    uow_factory, _, _ = _runtime(request)

    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )
        current_run_id = run.id

    return await _cancel_production_run(
        request,
        current_run_id,
        actor_id=await _actor_id(request),
    )


async def _artifact_view(
    request: Request,
    subject_id: UUID,
    stage: str,
) -> dict[str, Any]:
    uow_factory, _, _ = _runtime(request)

    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(status_code=404, detail="No production run found")

    return await _artifact_view_for_run(request, run.id, stage)


async def _artifact_view_for_run(
    request: Request,
    run_id: UUID,
    stage: str,
) -> dict[str, Any]:
    uow_factory, _, _ = _runtime(request)

    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="No production run found")

        artifact = (
            await current_publication_artifact(uow.production_artifacts, run_id)
            if stage == ProductionArtifactStage.PUBLICATION.value
            else await uow.production_artifacts.get_current(run_id, stage)
        )
        if not artifact:
            raise HTTPException(status_code=404, detail=f"{stage} artifact not found")

        store = getattr(request.app.state, "production_artifact_store", None)
        rendered = None
        canonical = None
        if store is not None:
            if artifact.rendered_blob_id is not None:
                rendered = await store.read_text(artifact.rendered_blob_id)
            if artifact.canonical_blob_id is not None:
                canonical = await store.read_json(artifact.canonical_blob_id)

        return {
            "artifact_id": str(artifact.id),
            "stage": artifact.stage.value,
            "version": artifact.version,
            "status": artifact.status.value,
            "reused": artifact.reused_from_artifact_id is not None,
            "reused_from_artifact_id": (
                str(artifact.reused_from_artifact_id)
                if artifact.reused_from_artifact_id is not None
                else None
            ),
            "reused_from_created_at": artifact.metadata.get("reused_from_created_at"),
            "metadata": artifact.metadata,
            "rendered_content": rendered,
            "canonical_content": canonical,
        }


@router.get("/subjects/{subject_id}/production/artifacts/references")
async def get_references_artifact(subject_id: UUID, request: Request) -> dict[str, Any]:
    return await _artifact_view(request, subject_id, "references")


@router.get("/subjects/{subject_id}/production/artifacts/extraction")
async def get_extraction_artifact(subject_id: UUID, request: Request) -> dict[str, Any]:
    return await _artifact_view(request, subject_id, "extraction")


@router.get("/subjects/{subject_id}/production/artifacts/synthesis")
async def get_synthesis_artifact(subject_id: UUID, request: Request) -> dict[str, Any]:
    return await _artifact_view(request, subject_id, "synthesis")


@router.get("/subjects/{subject_id}/production/artifacts/publication")
async def get_publication_artifact(subject_id: UUID, request: Request) -> dict[str, Any]:
    return await _artifact_view(request, subject_id, ProductionArtifactStage.PUBLICATION.value)


@router.get("/production/runs/{run_id}/artifacts/publication")
async def get_run_publication_artifact(run_id: UUID, request: Request) -> dict[str, Any]:
    return await _artifact_view_for_run(request, run_id, ProductionArtifactStage.PUBLICATION.value)


@router.post("/editions/{edition_id}/production")
async def start_edition_production(
    edition_id: UUID,
    request: Request,
    body: StartEditionProductionRequest | None = None,
) -> BatchStatus:
    # Idempotent: returns the existing active batch if one exists.
    payload = body or StartEditionProductionRequest()
    if payload.subject_ids is not None and not payload.subject_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one subject must be selected for production",
        )
    uow_factory, jobs, dispatcher = _runtime(request)
    service = EditionProductionService(uow_factory, _production_pacing(request))

    async with uow_factory() as uow:
        active_batch = await uow.edition_production_batches.get_active_for_edition(edition_id)
        if active_batch:
            return await _batch_status_view(uow, active_batch)

        groups = await uow.editorial_groups.list_for_edition(edition_id)
        eligible_order = _eligible_article_subject_ids(groups)

    if not eligible_order:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No selected articles found for edition",
        )

    if payload.subject_ids is not None:
        requested = set(payload.subject_ids)
        unknown = requested - set(eligible_order)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Some requested subjects are not selected articles",
            )
        subject_ids = [sid for sid in eligible_order if sid in requested]
    else:
        subject_ids = list(eligible_order)

    try:
        actor_id = await _actor_id(request)
        batch = await service.create_batch(
            edition_id=edition_id,
            subject_ids=subject_ids,
            actor_id=actor_id,
            correlation_id=get_correlation_id(),
        )

        # create_batch already made a run per subject and linked items to them;
        # start_next promotes the first one to RUNNING.
        first_run = await service.start_next(batch.id)
        if first_run is not None:
            if await _production_run_can_dispatch(uow_factory, first_run.id, batch_id=batch.id):
                parameters = ProductionStageParameters(
                    run_id=first_run.id,
                    expected_stage=SubjectProductionStage.SOURCES.value,
                    pipeline_generation=first_run.pipeline_generation,
                )
                job = await jobs.submit(
                    kind="production.subject.sources",
                    aggregate_type="subject",
                    aggregate_id=first_run.subject_id,
                    idempotency_key=production_stage_idempotency_key(
                        first_run, SubjectProductionStage.SOURCES
                    ),
                    correlation_id=get_correlation_id(),
                    input_parameters=parameters.model_dump(mode="json"),
                    max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
                    actor_id=actor_id,
                )
                if await _production_run_can_dispatch(uow_factory, first_run.id, batch_id=batch.id):
                    await dispatcher.dispatch(job.id)
                else:
                    await _cancel_non_terminal_run_jobs(
                        jobs,
                        [(first_run.id, first_run.subject_id)],
                        actor_id=actor_id,
                    )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    async with uow_factory() as uow:
        persisted_batch = await uow.edition_production_batches.get(batch.id)
        return await _batch_status_view(uow, persisted_batch or batch)


@router.get("/editions/{edition_id}/production")
async def get_edition_production(
    edition_id: UUID,
    request: Request,
) -> BatchStatus:
    # 404 here is the signal the UI uses to offer production.
    uow_factory, _, _ = _runtime(request)
    service = EditionProductionService(uow_factory)

    async with uow_factory() as uow:
        batch = await service.get_batch(edition_id)
        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No batch found for edition {edition_id}",
            )

        return await _batch_status_view(uow, batch)


@router.post("/editions/{edition_id}/production/{batch_id}/cancel")
async def cancel_edition_batch(
    edition_id: UUID,
    batch_id: UUID,
    request: Request,
) -> dict[str, Any]:
    uow_factory, jobs, _ = _runtime(request)
    actor_id = await _actor_id(request)

    service = EditionProductionService(uow_factory, _production_pacing(request))
    try:
        cancellation = await service.cancel_batch_with_result(
            edition_id,
            batch_id,
            actor_id=actor_id,
            correlation_id=get_correlation_id(),
        )
    except EditionProductionBatchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        ) from exc
    except EditionProductionBatchOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except StaleEditionProductionBatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "stale_production_batch",
                "message": str(exc),
            },
        ) from exc
    except ProductionBatchCancellationConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": exc.code,
                "message": str(exc),
                "status": exc.status.value,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": str(exc)},
        ) from exc

    await _cancel_non_terminal_run_jobs(
        jobs,
        cancellation.cancelled_runs,
        actor_id=actor_id,
    )

    return {
        "action": "cancel",
        "batch_id": str(cancellation.batch.id),
        "status": cancellation.batch.status.value,
        "edition_status": cancellation.edition.status.value,
        "edition_version": cancellation.edition.version,
    }

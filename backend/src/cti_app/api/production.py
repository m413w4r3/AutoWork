from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from cti_app.application.identity import IdentityProvider
from cti_app.application.jobs import DuplicateJobError, JobDispatcher, JobService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_resolver import current_publication_artifact
from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageParameters,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_read_model import BatchStatusReadService
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
    EditionProductionService,
    ProductionRunNotFoundError,
    SubjectProductionService,
)
from cti_app.domain.editorial import EditorialGroup, EditorialGroupStatus
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionBatchStatus,
    ProductionReuseInvalidation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
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


class StageStatus(BaseModel):
    status: str  # pending, running, succeeded, needs_review, failed
    version: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    reused: bool = False
    reused_from_artifact_id: UUID | None = None
    reused_from_created_at: str | None = None
    research_date: str | None = None


class ProductionStatus(BaseModel):
    subject_id: str
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

    if run.status is SubjectProductionStatus.RUNNING and not created:
        # Already in flight: never start it again, never re-prompt.
        return run, None

    if created or run.status is SubjectProductionStatus.QUEUED:
        # start_run persists and returns the RUNNING run; keep that object,
        # not the stale QUEUED one create_run returned.
        run = await service.start_run(run.id)

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
    await dispatcher.dispatch(job.id)
    return run, job.id


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
        snapshot_repository = getattr(uow, "production_input_snapshots", None)
        snapshot = await snapshot_repository.get_by_run(run.id) if snapshot_repository else None

        # Artifacts evidence the stages that produce one; SOURCES does not.
        artifacts = await uow.production_artifacts.list_for_run(run.id)
        artifacts_by_stage = {a.stage.value: a for a in artifacts}
        collections = await uow.source_collections.list_for_subject(subject_id)
        archived_sources = sum(1 for c in collections if c.state in _ARCHIVED_STATES)

        stages = build_stage_statuses(
            run,
            artifacts_by_stage,
            archived_sources=archived_sources,
            research_date=snapshot.research_date if snapshot else run.research_date,
        )
        completed_stages = completed_stage_count(stages)

        return ProductionStatus(
            subject_id=str(run.subject_id),
            title=(
                snapshot.subject_title
                if snapshot
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
        repository = getattr(uow, "production_reuse_invalidations", None)
        if repository is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "production_reuse_invalidation_unavailable"},
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
        await repository.add(invalidation)
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


@router.post("/subjects/{subject_id}/production/cancel")
async def cancel_production(
    subject_id: UUID,
    request: Request,
) -> dict[str, Any]:
    uow_factory, jobs, _ = _runtime(request)
    service = SubjectProductionService(uow_factory)

    async with uow_factory() as uow:
        run = await uow.subject_production_runs.get_current_for_subject(subject_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No production run found for subject {subject_id}",
            )

    cancelled = await service.cancel_run(run.id)
    await _cancel_non_terminal_run_jobs(
        jobs,
        [(cancelled.id, cancelled.subject_id)],
        actor_id=await _actor_id(request),
    )

    return {
        "action": "cancel",
        "run_id": str(run.id),
        "status": SubjectProductionStatus.CANCELLED.value,
    }


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

    if payload.subject_ids:
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
            await dispatcher.dispatch(job.id)
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
    runs_to_cancel: list[tuple[UUID, UUID]] = []

    async with uow_factory() as uow:
        batch = await uow.edition_production_batches.get_for_update(batch_id)
        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Batch {batch_id} not found",
            )

        if batch.edition_id != edition_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Batch does not belong to this edition",
            )

        batch.cancel(now=datetime.now(UTC))
        await uow.edition_production_batches.save(batch)
        items = await uow.edition_production_batch_items.list_for_batch(batch.id)
        for item in items:
            run = await uow.subject_production_runs.get_for_update(item.production_run_id)
            if run is None:
                continue
            runs_to_cancel.append((run.id, run.subject_id))
            if run.status is SubjectProductionStatus.QUEUED:
                run.mark_cancelled(now=datetime.now(UTC))
                await uow.subject_production_runs.save(run)
            elif run.status is SubjectProductionStatus.RUNNING:
                run.mark_cancelled(now=datetime.now(UTC))
                await uow.subject_production_runs.save(run)
        await uow.commit()

    await _cancel_non_terminal_run_jobs(
        jobs,
        runs_to_cancel,
        actor_id=await _actor_id(request),
    )

    return {
        "action": "cancel",
        "batch_id": str(batch.id),
        "status": batch.status.value,
    }

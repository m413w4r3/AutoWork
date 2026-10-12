"""Bounded, read-only projection of production control state."""

from __future__ import annotations

import base64
import binascii
from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from cti_app.application.edition_review import EditionReviewService
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_repairs import (
    ProductionRepairIssueService,
    repair_issue_execution_state,
)
from cti_app.application.production_stage_status import build_stage_statuses
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionBatchStatus,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_pipeline import production_stages
from cti_app.domain.typst_render import TypstRenderStatus


class ControlScopeKind(StrEnum):
    EDITION = "edition"
    RUN = "run"


class ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ControlScope(ControlModel):
    kind: ControlScopeKind
    edition_id: UUID
    run_id: UUID | None


class ControlStageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ControlPreviewState(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ControlActionPermission(StrEnum):
    PERMITTED = "permitted"
    UNKNOWN = "unknown"


class ControlActionId(StrEnum):
    CREATE_BATCH = "create_batch"
    PAUSE_BATCH = "pause_batch"
    RESUME_BATCH = "resume_batch"
    RETRY_RUN = "retry_run"
    RESUME_RUN = "resume_run"
    ACCEPT_PUBLICATION = "accept_publication"
    RETRY_RELEASE_RENDER = "retry_release_render"
    DECIDE_REPAIR = "decide_repair"


class ControlActionReason(StrEnum):
    BACKEND_RECHECKS_FENCES = "backend_rechecks_fences"
    RUN_STAGE_PRECONDITIONS_NOT_PROJECTED = "run_stage_preconditions_not_projected"


class ControlReleaseRenderStatus(StrEnum):
    NONE = "none"
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    RUNNING = "running"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class ControlRepairState(StrEnum):
    UNRESOLVED = "unresolved"
    REBUILD_PENDING = "rebuild_pending"


class ControlJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ControlBatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    CANCELLED = "cancelled"


class ControlAction(ControlModel):
    id: ControlActionId
    method: Literal["GET", "POST"]
    href: str
    permitted: ControlActionPermission
    reason: ControlActionReason | None = None
    requires_human_decision: bool = False


class ControlArtifact(ControlModel):
    id: UUID
    version: int
    input_hash: str
    status: ProductionArtifactStatus
    href: str


class ControlStage(ControlModel):
    stage: ProductionStage
    status: ControlStageStatus
    artifact: ControlArtifact | None = None


class ControlRunError(ControlModel):
    code: str | None = None
    details_ref: str


class ControlRun(ControlModel):
    run_id: UUID
    run_number: int
    pipeline_generation: int
    status: ProductionRunStatus
    current_stage: ProductionStage
    error: ControlRunError
    stages: list[ControlStage]
    revision_debt: ControlRevisionDebt


class ControlRevisionDebt(ControlModel):
    stale_enrichment_blocks: int | None = None
    latest_revision_artifact_id: UUID | None = None
    lineage_href: str | None = None


class ControlJob(ControlModel):
    id: UUID
    status: ControlJobStatus
    kind: str
    stage: ProductionStage | None = None
    pipeline_generation: int | None = None
    error_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ControlJobs(ControlModel):
    active: int
    failed: int
    waiting: int
    total: int
    items: list[ControlJob]
    href: str


class ControlSourceIssue(ControlModel):
    id: str
    kind: str
    reason_code: str | None = None
    stage: ProductionStage | None = None
    pipeline_generation: int
    href: str


class ControlSources(ControlModel):
    total: int
    archived: int
    eligible: int
    critical_unresolved: int | None = None
    critical_unresolved_items: list[ControlSourceIssue]
    href: str


class ControlRepair(ControlModel):
    id: str
    state: ControlRepairState
    kind: str
    blocking: bool
    rebuild_pending: bool
    stage: ProductionStage | None = None
    pipeline_generation: int
    href: str


class ControlRepairDebt(ControlModel):
    unresolved: int | None
    blocking: int | None
    pending_rebuild: int | None
    items: list[ControlRepair]
    href: str


class ControlPreview(ControlModel):
    state: ControlPreviewState
    artifact_id: UUID | None = None
    artifact_version: int | None = None
    input_hash: str | None = None
    render_identity: str | None = None
    render_id: UUID | None = None
    href: str | None = None


class ControlSubject(ControlModel):
    subject_id: UUID
    canonical_identity: UUID
    title: str
    tlp: TLP
    current_run: bool
    run: ControlRun | None
    jobs: ControlJobs
    sources: ControlSources
    repair_debt: ControlRepairDebt
    preview: ControlPreview
    actions: list[ControlAction]


class ControlSubjects(ControlModel):
    total: int
    items: list[ControlSubject]
    next_cursor: str | None


class ControlEdition(ControlModel):
    id: UUID
    state: EditionStatus
    version: int
    period_start: str
    period_end: str
    country_code: str
    tlp: TLP


class ControlBatch(ControlModel):
    batch_id: UUID
    status: ControlBatchStatus
    phase: ProductionBatchPhase
    paused_at: datetime | None = None
    href: str


class ControlBatches(ControlModel):
    active: ControlBatch | None
    recent: list[ControlBatch]
    href: str


class ControlSelection(ControlModel):
    snapshot_id: UUID | None
    snapshot_version: int | None
    selected_count: int
    href: str


class ControlReview(ControlModel):
    can_accept: bool
    blocking_subjects: int
    unresolved_repairs: int
    pending_rebuild: int
    href: str


class ControlRelease(ControlModel):
    manifest_id: UUID | None
    manifest_sha256: str | None
    release_id: UUID | None
    render_status: ControlReleaseRenderStatus
    pdf_available: bool
    ioc_export_href: str


class ProductionControlView(ControlModel):
    schema_version: Literal["agent-control.v1"] = "agent-control.v1"
    as_of: datetime
    scope: ControlScope
    edition: ControlEdition
    selection: ControlSelection
    batch: ControlBatches
    subjects: ControlSubjects
    actions: list[ControlAction]
    review: ControlReview
    release: ControlRelease


class InvalidControlCursorError(ValueError):
    pass


def _encode_cursor(subject_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"v1:{subject_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> UUID | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(cursor + padding).decode()
        version, value = decoded.split(":", 1)
        if version != "v1":
            raise ValueError
        return UUID(value)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidControlCursorError("invalid_control_cursor") from exc


def _latest_run(runs: list[ProductionRun]) -> ProductionRun | None:
    if not runs:
        return None
    # Keep the same current-run identity as get_current_for_subject(): the
    # latest allocated run number, independent of its execution state.
    return max(runs, key=lambda run: (run.run_number, run.created_at, run.id.int))


def _latest_artifacts(artifacts: list[ProductionArtifact]) -> dict[str, ProductionArtifact]:
    selected: dict[str, ProductionArtifact] = {}
    for artifact in artifacts:
        stage = artifact.stage.value
        current = selected.get(stage)
        if current is None or (artifact.version, artifact.id.int) > (
            current.version,
            current.id.int,
        ):
            selected[stage] = artifact
    return selected


def _job_run_id(job: Any) -> UUID | None:
    value = job.input_parameters.get("run_id")
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _action(
    action_id: ControlActionId,
    href: str,
    *,
    requires_human_decision: bool = False,
) -> ControlAction:
    return ControlAction(
        id=action_id,
        method="POST",
        href=href,
        permitted=ControlActionPermission.UNKNOWN,
        reason=ControlActionReason.BACKEND_RECHECKS_FENCES,
        requires_human_decision=requires_human_decision,
    )


class ProductionControlViewService:
    """Builds control summaries from canonical read repositories and services."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        *,
        repair_issue_service: ProductionRepairIssueService,
        review_service: EditionReviewService,
        publication_service: Any,
    ) -> None:
        self._uow_factory = uow_factory
        self._repair_issues = repair_issue_service
        self._review = review_service
        self._publication = publication_service

    async def edition_page(
        self, edition_id: UUID, *, cursor: str | None, limit: int
    ) -> ProductionControlView:
        after_id = _decode_cursor(cursor)
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise LookupError("edition_not_found")
            subjects, total = await uow.subjects.list_page_for_edition(
                edition_id, after_id=after_id, limit=limit + 1
            )
            has_more = len(subjects) > limit
            page_subjects = list(subjects[:limit])
            subject_ids = [subject.id for subject in page_subjects]
            runs = list(await uow.production_runs.list_for_subjects(subject_ids))
            active_batch = await uow.edition_production_batches.get_active_for_edition(edition_id)
            recent_batches = list(
                await uow.edition_production_batches.list_recent_for_edition(edition_id, 10)
            )
            artifacts = list(await uow.production_artifacts.list_for_runs([run.id for run in runs]))
            collections = list(await uow.source_collections.list_for_subjects(subject_ids))
            jobs = list(await uow.jobs.list_for_aggregates("subject", subject_ids))
            publication_ids = [
                artifact.id
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.PUBLICATION
            ]
            renders = list(await uow.publication_renders.list_latest_for_artifacts(publication_ids))
            selection_snapshot = await uow.discovery_snapshots.get_active(edition_id)
        return await self._assemble(
            edition=edition,
            scope_kind=ControlScopeKind.EDITION,
            runs=runs,
            subjects=page_subjects,
            total=total,
            has_more=has_more,
            active_batch=active_batch,
            recent_batches=recent_batches,
            artifacts=artifacts,
            collections=collections,
            jobs=jobs,
            renders=renders,
            selection_snapshot=selection_snapshot,
            selected_run_id=None,
        )

    async def run_view(self, run_id: UUID) -> ProductionControlView:
        async with self._uow_factory() as uow:
            run = await uow.production_runs.get(run_id)
            if run is None:
                raise LookupError("run_not_found")
            edition = await uow.editions.get(run.edition_id)
            subject = await uow.subjects.get(run.subject_id)
            if edition is None or subject is None:
                raise LookupError("run_identity_not_found")
            history = list(await uow.production_runs.list_for_subject(run.subject_id))
            current = _latest_run(history)
            active_batch = await uow.edition_production_batches.get_active_for_edition(
                run.edition_id
            )
            recent_batches = list(
                await uow.edition_production_batches.list_recent_for_edition(run.edition_id, 10)
            )
            artifacts = list(await uow.production_artifacts.list_for_run(run.id))
            collections = list(await uow.source_collections.list_for_subjects([subject.id]))
            jobs = list(await uow.jobs.list_for_aggregates("subject", [subject.id]))
            publication_ids = [
                artifact.id
                for artifact in artifacts
                if artifact.stage is ProductionArtifactStage.PUBLICATION
            ]
            renders = list(await uow.publication_renders.list_latest_for_artifacts(publication_ids))
            selection_snapshot = await uow.discovery_snapshots.get_active(run.edition_id)
        return await self._assemble(
            edition=edition,
            scope_kind=ControlScopeKind.RUN,
            runs=[run],
            subjects=[subject],
            total=1,
            has_more=False,
            active_batch=active_batch,
            recent_batches=recent_batches,
            artifacts=artifacts,
            collections=collections,
            jobs=jobs,
            renders=renders,
            selection_snapshot=selection_snapshot,
            selected_run_id=run_id,
            current_run_id=current.id if current is not None else None,
        )

    async def _assemble(
        self,
        *,
        edition: Any,
        scope_kind: ControlScopeKind,
        runs: list[ProductionRun],
        subjects: list[Any],
        total: int,
        has_more: bool,
        active_batch: Any,
        recent_batches: list[Any],
        artifacts: list[ProductionArtifact],
        collections: list[Any],
        jobs: list[Any],
        renders: list[Any],
        selection_snapshot: Any | None,
        selected_run_id: UUID | None,
        current_run_id: UUID | None = None,
    ) -> ProductionControlView:
        runs_by_subject: dict[UUID, list[ProductionRun]] = defaultdict(list)
        artifacts_by_run: dict[UUID, list[ProductionArtifact]] = defaultdict(list)
        collections_by_subject: dict[UUID, list[Any]] = defaultdict(list)
        jobs_by_subject: dict[UUID, list[Any]] = defaultdict(list)
        renders_by_artifact = {render.publication_artifact_id: render for render in renders}
        for run in runs:
            runs_by_subject[run.subject_id].append(run)
        for artifact in artifacts:
            artifacts_by_run[artifact.production_run_id].append(artifact)
        for collection in collections:
            collections_by_subject[collection.subject_id].append(collection)
        for job in jobs:
            jobs_by_subject[job.aggregate_id].append(job)

        current_runs = {
            subject_id: _latest_run(subject_runs)
            for subject_id, subject_runs in runs_by_subject.items()
        }
        if scope_kind is ControlScopeKind.RUN and selected_run_id is not None:
            projection_runs = [run for run in runs if run.id == selected_run_id]
        else:
            projection_runs = [run for run in current_runs.values() if run is not None]
        projection_run_ids = {run.id for run in projection_runs}

        # These existing services own the repair and Review predicates used by
        # the corresponding read and mutation routes. They are called once per
        # edition, independent of the page size.
        review = await self._review.get(edition.id)
        review_by_subject = {item.subject_id: item for item in review.items}
        issue_views = await self._repair_issues.list_issue_views(edition.id)
        supplemental = await self._repair_issues.list_supplemental_source_issues(edition.id)
        source_failures_getter = getattr(
            self._repair_issues, "list_source_extraction_failure_issues", None
        )
        source_failures = (
            await source_failures_getter(edition.id) if callable(source_failures_getter) else ()
        )
        repair_views = [*issue_views, *supplemental, *source_failures]
        critical_by_run: dict[UUID, list[Any]] = defaultdict(list)
        repair_counts: dict[UUID, dict[str, int]] = defaultdict(
            lambda: {"unresolved": 0, "blocking": 0, "pending_rebuild": 0}
        )
        repair_items_by_run: dict[UUID, list[ControlRepair]] = defaultdict(list)
        for issue in repair_views:
            issue_run_id = getattr(issue, "production_run_id", None)
            if issue_run_id not in projection_run_ids:
                continue
            decision = getattr(issue, "effective_decision", None)
            execution = repair_issue_execution_state(issue, decision)
            if not execution.resolved or execution.rebuild_required:
                repair_counts[issue_run_id]["unresolved"] += int(not execution.resolved)
                repair_counts[issue_run_id]["blocking"] += int(execution.blocking)
                repair_counts[issue_run_id]["pending_rebuild"] += int(execution.rebuild_required)
                if len(repair_items_by_run[issue_run_id]) < 20:
                    subject_id = getattr(issue, "subject_id", None)
                    if not isinstance(subject_id, UUID):
                        subject_id = next(
                            (run.subject_id for run in projection_runs if run.id == issue_run_id),
                            None,
                        )
                    href = (
                        f"/api/subjects/{subject_id}/production/repairs/{issue.repair_key}"
                        if subject_id is not None
                        else f"/api/production/runs/{issue_run_id}"
                    )
                    stage = (
                        ProductionStage.REFERENCES
                        if getattr(issue, "kind", None)
                        in {"supplemental_source_unarchived", "source_extraction_failed"}
                        else None
                    )
                    repair_items_by_run[issue_run_id].append(
                        ControlRepair(
                            id=str(issue.repair_key),
                            state=(
                                ControlRepairState.UNRESOLVED
                                if not execution.resolved
                                else ControlRepairState.REBUILD_PENDING
                            ),
                            kind=getattr(issue.kind, "value", str(issue.kind)),
                            blocking=execution.blocking,
                            rebuild_pending=execution.rebuild_required,
                            stage=stage,
                            pipeline_generation=int(
                                getattr(issue, "observed_pipeline_generation", 0)
                            ),
                            href=href,
                        )
                    )
            criticality = getattr(issue, "source_criticality", None)
            criticality_value = getattr(criticality, "value", criticality)
            if criticality_value == "critical" and not execution.resolved:
                critical_by_run[issue_run_id].append(issue)

        subjects_views: list[ControlSubject] = []
        release_status = await self._publication.release_status(edition.id)
        root_actions = self._edition_actions(edition, active_batch, review, release_status)
        for subject in subjects:
            subject_runs = runs_by_subject.get(subject.id, [])
            selected_subject_run: ProductionRun | None = (
                next(
                    (candidate for candidate in subject_runs if candidate.id == selected_run_id),
                    None,
                )
                if selected_run_id is not None
                else _latest_run(subject_runs)
            )
            if selected_subject_run is None:
                current = False
                run_view = None
                jobs_view = ControlJobs(
                    active=0,
                    failed=0,
                    waiting=0,
                    total=0,
                    items=[],
                    href=f"/api/jobs?aggregate_type=subject&aggregate_id={subject.id}",
                )
                source_items: list[ControlSourceIssue] = []
                sources_view = self._sources_view(
                    subject,
                    collections_by_subject[subject.id],
                    0,
                    source_items,
                )
                repair_view = ControlRepairDebt(
                    unresolved=None,
                    blocking=None,
                    pending_rebuild=None,
                    items=[],
                    href=f"/api/subjects/{subject.id}/production/repairs",
                )
                preview = ControlPreview(state=ControlPreviewState.UNKNOWN)
                actions: list[ControlAction] = []
            else:
                subject_current_run = current_runs.get(subject.id)
                current = (
                    selected_subject_run.id == current_run_id
                    if current_run_id is not None
                    else subject_current_run is not None
                    and selected_subject_run.id == subject_current_run.id
                )
                run_artifacts = artifacts_by_run.get(selected_subject_run.id, [])
                current_artifacts = _latest_artifacts(run_artifacts)
                run_view = self._run_view(selected_subject_run, current_artifacts, current)
                matching_jobs = [
                    job
                    for job in jobs_by_subject.get(subject.id, [])
                    if _job_run_id(job) == selected_subject_run.id
                ]
                jobs_view = self._jobs_view(subject.id, matching_jobs)
                source_items = [
                    ControlSourceIssue(
                        id=str(issue.repair_key),
                        kind=getattr(issue.kind, "value", str(issue.kind)),
                        reason_code=(
                            getattr(issue, "failure_code", None)
                            or getattr(issue, "reason_code", None)
                        ),
                        stage=(
                            ProductionStage.EXTRACTION
                            if getattr(issue, "kind", None) == "source_extraction_failed"
                            else ProductionStage.REFERENCES
                        ),
                        pipeline_generation=int(
                            getattr(
                                issue,
                                "observed_pipeline_generation",
                                selected_subject_run.pipeline_generation,
                            )
                        ),
                        href=f"/api/subjects/{subject.id}/production/repairs/{issue.repair_key}",
                    )
                    for issue in critical_by_run.get(selected_subject_run.id, [])
                ][:20]
                sources_view = self._sources_view(
                    subject,
                    collections_by_subject[subject.id],
                    len(critical_by_run.get(selected_subject_run.id, [])),
                    source_items,
                )
                counts = repair_counts.get(selected_subject_run.id)
                review_item = review_by_subject.get(subject.id) if current else None
                pending_rebuild = (
                    review_item.pending_rebuild_count
                    if review_item is not None
                    else counts["pending_rebuild"]
                    if counts is not None and current
                    else None
                )
                repair_view = ControlRepairDebt(
                    unresolved=(
                        counts["unresolved"]
                        if counts is not None and current
                        else 0
                        if current
                        else None
                    ),
                    blocking=(
                        counts["blocking"]
                        if counts is not None and current
                        else 0
                        if current
                        else None
                    ),
                    pending_rebuild=pending_rebuild,
                    items=repair_items_by_run.get(selected_subject_run.id, [])[:20]
                    if current
                    else [],
                    href=f"/api/subjects/{subject.id}/production/repairs",
                )
                pub_artifact = current_artifacts.get(ProductionArtifactStage.PUBLICATION.value)
                render = renders_by_artifact.get(pub_artifact.id) if pub_artifact else None
                preview = self._preview(subject.id, pub_artifact, render, current)
                actions = self._run_actions(
                    edition,
                    active_batch,
                    selected_subject_run,
                    current,
                    review_by_subject.get(subject.id) if current else None,
                )
                actions.extend(
                    self._repair_actions(
                        edition,
                        selected_subject_run,
                        current,
                        repair_view.items,
                    )
                )
            subjects_views.append(
                ControlSubject(
                    subject_id=subject.id,
                    canonical_identity=subject.id,
                    title=subject.title[:200],
                    tlp=subject.tlp,
                    current_run=current,
                    run=run_view,
                    jobs=jobs_view,
                    sources=sources_view,
                    repair_debt=repair_view,
                    preview=preview,
                    actions=actions,
                )
            )

        active_view = (
            self._batch_view(edition.id, active_batch) if active_batch is not None else None
        )
        recent_views = [self._batch_view(edition.id, batch) for batch in recent_batches[:10]]
        next_cursor = _encode_cursor(subjects[-1].id) if has_more and subjects else None
        return ProductionControlView(
            as_of=datetime.now(UTC),
            scope=ControlScope(
                kind=scope_kind,
                edition_id=edition.id,
                run_id=selected_run_id,
            ),
            edition=ControlEdition(
                id=edition.id,
                state=edition.state,
                version=edition.version,
                period_start=edition.period_start.isoformat(),
                period_end=edition.period_end.isoformat(),
                country_code=edition.country_code,
                tlp=edition.tlp,
            ),
            selection=ControlSelection(
                snapshot_id=selection_snapshot.id if selection_snapshot is not None else None,
                snapshot_version=(
                    selection_snapshot.version if selection_snapshot is not None else None
                ),
                selected_count=total,
                href=f"/api/editions/{edition.id}/selection",
            ),
            batch=ControlBatches(
                active=active_view,
                recent=recent_views,
                href=f"/api/editions/{edition.id}/production",
            ),
            subjects=ControlSubjects(total=total, items=subjects_views, next_cursor=next_cursor),
            actions=root_actions,
            review=ControlReview(
                can_accept=review.can_accept,
                blocking_subjects=sum(item.blocking for item in review.items),
                unresolved_repairs=review.unresolved_repair_count,
                pending_rebuild=review.pending_rebuild_count,
                href=f"/api/editions/{edition.id}/review",
            ),
            release=self._release_view(edition.id, release_status),
        )

    def _run_view(
        self,
        run: ProductionRun,
        artifacts: dict[str, ProductionArtifact],
        current: bool,
    ) -> ControlRun:
        raw_stages = build_stage_statuses(run, artifacts, research_date=run.research_date)
        stages: list[ControlStage] = []
        for stage in production_stages():
            artifact_stage = {
                ProductionStage.REFERENCES: ProductionArtifactStage.REFERENCES,
                ProductionStage.EXTRACTION: ProductionArtifactStage.EXTRACTION,
                ProductionStage.RELEVANCE_PROJECTION: ProductionArtifactStage.RELEVANCE_PROJECTION,
                ProductionStage.SYNTHESIS: ProductionArtifactStage.SYNTHESIS,
                ProductionStage.EDITORIAL_ENRICHMENT: ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionStage.ASSEMBLY: ProductionArtifactStage.PUBLICATION,
            }.get(stage)
            artifact = artifacts.get(artifact_stage.value) if artifact_stage else None
            if artifact is None:
                artifact_view = None
            else:
                if current and artifact.status is not ProductionArtifactStatus.STALE:
                    href = (
                        f"/api/subjects/{run.subject_id}/production/artifacts/"
                        f"{artifact.stage.value}"
                    )
                elif (
                    artifact.stage is ProductionArtifactStage.PUBLICATION
                    and artifact.status is not ProductionArtifactStatus.STALE
                ):
                    href = f"/api/production/runs/{run.id}/artifacts/publication"
                else:
                    href = f"/api/production/runs/{run.id}"
                artifact_view = ControlArtifact(
                    id=artifact.id,
                    version=artifact.version,
                    input_hash=artifact.input_hash,
                    status=artifact.status,
                    href=href,
                )
            stages.append(
                ControlStage(
                    stage=stage,
                    status=ControlStageStatus(raw_stages[stage.value]["status"]),
                    artifact=artifact_view,
                )
            )
        enrichment = artifacts.get(ProductionArtifactStage.EDITORIAL_ENRICHMENT.value)
        stale_blocks = None
        if enrichment is not None:
            count = enrichment.metadata.get("stale_enrichment_blocks")
            if isinstance(count, int) and count >= 0:
                stale_blocks = count
        return ControlRun(
            run_id=run.id,
            run_number=run.run_number,
            pipeline_generation=run.pipeline_generation,
            status=run.status,
            current_stage=run.current_stage,
            error=ControlRunError(
                code=run.error_code,
                details_ref=f"/api/production/runs/{run.id}",
            ),
            stages=stages,
            revision_debt=ControlRevisionDebt(
                stale_enrichment_blocks=stale_blocks,
                latest_revision_artifact_id=enrichment.id if enrichment else None,
                lineage_href=(
                    f"/api/production/artifacts/{enrichment.id}/revision-lineage"
                    if enrichment is not None
                    else None
                ),
            ),
        )

    def _jobs_view(self, subject_id: UUID, jobs: list[Any]) -> ControlJobs:
        jobs.sort(key=lambda job: (job.created_at, job.id.int), reverse=True)
        total = len(jobs)
        items = []
        for job in jobs[:20]:
            parameters = job.input_parameters
            stage_value = parameters.get("stage") or parameters.get("current_stage")
            try:
                stage = ProductionStage(stage_value) if stage_value is not None else None
            except ValueError:
                stage = None
            generation = parameters.get("pipeline_generation")
            if type(generation) is not int:
                generation = None
            items.append(
                ControlJob(
                    id=job.id,
                    status=ControlJobStatus(job.status.value),
                    kind=job.kind[:80],
                    stage=stage,
                    pipeline_generation=generation,
                    error_code=job.error_code,
                    created_at=job.created_at,
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                )
            )
        return ControlJobs(
            active=sum(job.status.value == "running" for job in jobs),
            failed=sum(job.status.value == "failed" for job in jobs),
            waiting=sum(job.status.value in {"queued", "waiting_human"} for job in jobs),
            total=total,
            items=items,
            href=f"/api/jobs?aggregate_type=subject&aggregate_id={subject_id}",
        )

    def _sources_view(
        self,
        subject: Any,
        collections: list[Any],
        critical_count: int | None,
        critical_items: list[ControlSourceIssue],
    ) -> ControlSources:
        archived_states = {
            CollectionState.ARCHIVED,
            CollectionState.EXTRACTED,
            CollectionState.COMPLETED,
        }
        archived = sum(collection.state in archived_states for collection in collections)
        eligible = sum(
            collection.state in archived_states and not collection.do_not_submit
            for collection in collections
        )
        return ControlSources(
            total=len(collections),
            archived=archived,
            eligible=eligible,
            critical_unresolved=critical_count,
            critical_unresolved_items=critical_items,
            href=f"/api/subjects/{subject.id}/production/artifacts/references",
        )

    def _preview(
        self,
        subject_id: UUID,
        artifact: ProductionArtifact | None,
        render: Any,
        current: bool,
    ) -> ControlPreview:
        if artifact is None:
            return ControlPreview(state=ControlPreviewState.UNAVAILABLE)
        href = f"/api/subjects/{subject_id}/publication/preview?artifact_id={artifact.id}"
        if not current:
            state = ControlPreviewState.STALE
        elif artifact.status is not ProductionArtifactStatus.VERIFIED:
            state = (
                ControlPreviewState.STALE
                if artifact.status is ProductionArtifactStatus.STALE
                else ControlPreviewState.UNAVAILABLE
            )
        elif render is None:
            state = ControlPreviewState.UNKNOWN
        elif render.status is TypstRenderStatus.SUCCEEDED and render.output_blob_id is not None:
            state = ControlPreviewState.AVAILABLE
        elif render.status is TypstRenderStatus.FAILED:
            state = ControlPreviewState.UNAVAILABLE
        else:
            state = ControlPreviewState.UNKNOWN
        return ControlPreview(
            state=state,
            artifact_id=artifact.id,
            artifact_version=artifact.version,
            input_hash=artifact.input_hash,
            render_identity=render.input_hash if render is not None else None,
            render_id=render.id if render is not None else None,
            href=href,
        )

    def _run_actions(
        self,
        edition: Any,
        active_batch: Any,
        run: ProductionRun,
        current: bool,
        review_item: Any | None,
    ) -> list[ControlAction]:
        if edition.state is EditionStatus.ARCHIVED or not current or active_batch is not None:
            return []
        base = f"/api/subjects/{run.subject_id}/production"
        if review_item is not None and review_item.can_retry:
            return [
                ControlAction(
                    id=ControlActionId.RETRY_RUN,
                    method="POST",
                    href=f"{base}/retry",
                    permitted=ControlActionPermission.UNKNOWN,
                    reason=ControlActionReason.RUN_STAGE_PRECONDITIONS_NOT_PROJECTED,
                )
            ]
        if review_item is not None and review_item.can_resume:
            return [
                ControlAction(
                    id=ControlActionId.RESUME_RUN,
                    method="POST",
                    href=f"{base}/resume",
                    permitted=ControlActionPermission.UNKNOWN,
                    reason=ControlActionReason.RUN_STAGE_PRECONDITIONS_NOT_PROJECTED,
                )
            ]
        return []

    def _repair_actions(
        self,
        edition: Any,
        run: ProductionRun,
        current: bool,
        repairs: list[ControlRepair],
    ) -> list[ControlAction]:
        if edition.state is EditionStatus.ARCHIVED or not current:
            return []
        return [
            ControlAction(
                id=ControlActionId.DECIDE_REPAIR,
                method="POST",
                href=f"{repair.href}/decision",
                permitted=ControlActionPermission.UNKNOWN,
                reason=ControlActionReason.BACKEND_RECHECKS_FENCES,
                requires_human_decision=True,
            )
            for repair in repairs
            if repair.state is ControlRepairState.UNRESOLVED
        ]

    def _edition_actions(
        self, edition: Any, active_batch: Any, review: Any, release: Any
    ) -> list[ControlAction]:
        if edition.state is EditionStatus.ARCHIVED:
            return []
        actions: list[ControlAction] = []
        if active_batch is None:
            actions.append(
                _action(
                    ControlActionId.CREATE_BATCH,
                    f"/api/editions/{edition.id}/production/batches",
                )
            )
        elif active_batch.status is ProductionBatchStatus.RUNNING:
            actions.append(
                _action(
                    ControlActionId.PAUSE_BATCH,
                    f"/api/editions/{edition.id}/production/{active_batch.id}/pause",
                )
            )
        elif active_batch.status is ProductionBatchStatus.PAUSED:
            actions.append(
                _action(
                    ControlActionId.RESUME_BATCH,
                    f"/api/editions/{edition.id}/production/{active_batch.id}/resume",
                )
            )
        if review.can_accept:
            actions.append(
                ControlAction(
                    id=ControlActionId.ACCEPT_PUBLICATION,
                    method="POST",
                    href=f"/api/editions/{edition.id}/publication/accept",
                    permitted=ControlActionPermission.UNKNOWN,
                    reason=ControlActionReason.BACKEND_RECHECKS_FENCES,
                    requires_human_decision=True,
                )
            )
        if release.can_retry_render:
            actions.append(
                _action(
                    ControlActionId.RETRY_RELEASE_RENDER,
                    f"/api/editions/{edition.id}/release/render",
                )
            )
        return actions

    def _batch_view(self, edition_id: UUID, batch: Any) -> ControlBatch:
        return ControlBatch(
            batch_id=batch.id,
            status=ControlBatchStatus(batch.status.value),
            phase=batch.phase.value,
            paused_at=batch.paused_at,
            href=f"/api/editions/{edition_id}/production",
        )

    def _release_view(self, edition_id: UUID, release: Any) -> ControlRelease:
        return ControlRelease(
            manifest_id=release.manifest_id,
            manifest_sha256=release.manifest_sha256,
            release_id=release.release.id if release.release is not None else None,
            render_status=ControlReleaseRenderStatus(release.render_status.value),
            pdf_available=release.pdf_available,
            ioc_export_href=f"/api/editions/{edition_id}/release/iocs.zip",
        )

"""Service orchestrating subject production workflow."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from cti_app.application.persistence import (
    ProductionUnitOfWork,
    ProductionUnitOfWorkFactory,
)
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.domain.editions import EditionAuditEvent, EditionStatus
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionBatchPhase,
    ProductionBatchStatus,
    ProductionInputSnapshot,
    ProductionInputSource,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    production_stages,
)

_SOURCE_ROLE_ORDER = {
    "primary": 0,
    "independent": 1,
    "relay": 2,
    "aggregator": 3,
    "social": 4,
    "unknown": 9,
}


class ProductionRunNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class SubjectProductionRetryResult:
    run: SubjectProductionRun
    staled_artifacts: list[str]
    previous_status: SubjectProductionStatus
    previous_stage: SubjectProductionStage
    old_generation: int

    def __iter__(self) -> Iterator[SubjectProductionRun | list[str]]:
        """Keep the former ``run, staled`` unpacking contract for callers."""
        yield self.run
        yield self.staled_artifacts


async def capture_production_input_snapshot(
    uow: ProductionUnitOfWork,
    *,
    production_run_id: UUID,
    subject_id: UUID,
    edition_id: UUID,
    research_date: date,
    captured_at: datetime,
) -> ProductionInputSnapshot:
    """Capture the selected editorial input and resolve its exact candidates."""
    group = await uow.editorial_groups.get_by_subject(subject_id)
    if group is None or group.edition_id != edition_id:
        raise ValueError("production_snapshot_editorial_group_missing")

    editions = getattr(uow, "editions", None)
    edition = await editions.get(edition_id) if editions is not None else None
    if edition is None:
        raise ValueError("production_snapshot_edition_missing")

    discovery_batches = getattr(uow, "discovery_batches", None)
    batches = {}
    if discovery_batches is not None:
        batches = {
            batch.id: batch for batch in await discovery_batches.list_for_edition(edition_id)
        }

    candidates: list[object] = []
    by_url: dict[str, tuple[tuple[object, ...], ProductionInputSource]] = {}
    for reference in sorted(
        group.candidate_references, key=lambda item: (str(item.batch_id), str(item.candidate_id))
    ):
        batch = batches.get(reference.batch_id)
        candidate = (
            next((item for item in batch.candidates if item.id == reference.candidate_id), None)
            if batch is not None
            else None
        )
        if candidate is None:
            raise ValueError("production_snapshot_source_candidate_missing")
        candidates.append(candidate)
        for source in candidate.sources:
            captured = ProductionInputSource(
                batch_id=reference.batch_id,
                candidate_id=reference.candidate_id,
                source_candidate_id=source.id,
                canonical_url=source.canonical_url,
                role=source.role,
                title=source.title,
                publisher=source.publisher,
                published_at=source.published_at,
                tlp=source.tlp,
                sensitivity=source.sensitivity,
                external_llm_allowed=source.external_llm_allowed,
            )
            rank = (
                _SOURCE_ROLE_ORDER.get(source.role.value, 9),
                source.title.casefold(),
                source.publisher.casefold(),
                source.published_at or date.max,
                str(reference.batch_id),
                str(reference.candidate_id),
                str(source.id),
            )
            previous = by_url.get(captured.canonical_url)
            if previous is None or rank < previous[0]:
                by_url[captured.canonical_url] = (rank, captured)

    actor_values: dict[str, str] = {}
    for candidate in candidates:
        for value in (
            getattr(candidate, "actor_or_campaign", ""),
            *getattr(candidate, "actors", ()),
            *getattr(candidate, "campaigns", ()),
        ):
            cleaned = str(value).strip()
            if cleaned and cleaned.casefold() not in {"unknown", "n/a", "none"}:
                actor_values.setdefault(cleaned.casefold(), cleaned)

    core_sources = tuple(
        item[1] for item in sorted(by_url.values(), key=lambda value: value[1].canonical_url)
    )
    return ProductionInputSnapshot(
        production_run_id=production_run_id,
        subject_id=subject_id,
        edition_id=edition_id,
        editorial_group_id=group.id,
        editorial_group_version=group.version,
        subject_title=group.title,
        subject_description=group.grouping_justification,
        actor_or_campaign=" · ".join(actor_values[key] for key in sorted(actor_values)),
        period_start=edition.period_start,
        period_end=edition.period_end,
        research_date=research_date,
        core_sources=core_sources,
        captured_at=captured_at,
    )


class SubjectProductionService:
    def __init__(self, uow_factory: ProductionUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def create_run(
        self,
        subject_id: UUID,
        edition_id: UUID,
    ) -> tuple[SubjectProductionRun, bool]:
        """Create a production run for a subject.

        Returns the run and whether it was created by this call, so the caller
        knows whether to start it and submit a job — two concurrent POSTs must
        yield one logical run and one logical job.
        """
        async with self._uow_factory() as uow:
            existing = await uow.subject_production_runs.get_current_for_subject(subject_id)
            if existing and existing.status in (
                SubjectProductionStatus.QUEUED,
                SubjectProductionStatus.RUNNING,
            ):
                return existing, False

            allocator = getattr(uow.subject_production_runs, "allocate_next_run_number", None)
            if allocator is not None:
                next_run_number = await allocator(subject_id)
            else:
                # Lightweight test repositories predating the SQL helper.
                all_runs = await uow.subject_production_runs.list_for_edition(edition_id)
                next_run_number = 1 + sum(1 for r in all_runs if r.subject_id == subject_id)

            run = SubjectProductionRun(
                subject_id=subject_id,
                edition_id=edition_id,
                run_number=next_run_number,
            )
            await uow.subject_production_runs.add(run)
            snapshot_repository = getattr(uow, "production_input_snapshots", None)
            if snapshot_repository is not None:
                assert run.research_date is not None
                snapshot = await capture_production_input_snapshot(
                    uow,
                    production_run_id=run.id,
                    subject_id=subject_id,
                    edition_id=edition_id,
                    research_date=run.research_date,
                    captured_at=run.created_at,
                )
                await snapshot_repository.add(snapshot)
            await uow.commit()
            return run, True

    async def start_run(self, run_id: UUID) -> SubjectProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.start_running(now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def advance_stage(self, run_id: UUID) -> SubjectProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.advance_stage(now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def mark_ready(self, run_id: UUID) -> SubjectProductionRun:
        """Mark production run as ready (assembly complete + QA passed)."""
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.mark_ready(now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def mark_needs_review(
        self,
        run_id: UUID,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> SubjectProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.mark_needs_review(
                code=code, message=message, details=details, now=datetime.now(UTC)
            )
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def mark_failed(
        self,
        run_id: UUID,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> SubjectProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.mark_failed(code=code, message=message, details=details, now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def cancel_run(self, run_id: UUID) -> SubjectProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.mark_cancelled(now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def retry_from_stage(
        self, run_id: UUID, stage: SubjectProductionStage
    ) -> SubjectProductionRetryResult:
        async with self._uow_factory() as uow:
            # A user retry must acquire locks in the same order as publication
            # freeze: Edition first, then SubjectProductionRun.  The initial
            # read only discovers the edition that owns the run.
            initial_run = await uow.subject_production_runs.get(run_id)
            if not initial_run:
                raise ProductionRunNotFoundError(str(run_id))

            editions = getattr(uow, "editions", None)
            if editions is not None:
                get_edition_for_update = getattr(editions, "get_for_update", None)
                edition = (
                    await get_edition_for_update(initial_run.edition_id)
                    if get_edition_for_update is not None
                    else await editions.get(initial_run.edition_id)
                )
                if edition is None:
                    raise ValueError("edition_not_found")
                if edition.status not in {EditionStatus.PRODUCTION, EditionStatus.REVIEW}:
                    raise ValueError("edition_frozen_for_publication")

                # A manifest is immutable evidence of a freeze.  Keep this
                # check under the Edition lock so a retry cannot race with the
                # transaction that creates the manifest.
                manifests = getattr(uow, "publication_manifests", None)
                if (
                    manifests is not None
                    and await manifests.get_latest_for_edition(initial_run.edition_id) is not None
                ):
                    raise ValueError("edition_frozen_for_publication")

                result = await self._retry_from_stage_in_uow(
                    uow,
                    run_id,
                    stage,
                    expected_edition_id=initial_run.edition_id,
                )
            else:
                # Lightweight non-database callers predating the Edition port
                # still use the shared transaction core.  The real production
                # UoW always exposes ``editions`` and therefore takes the
                # protected path above.
                result = await self._retry_from_stage_in_uow(uow, run_id, stage)
            await uow.commit()
            return result

    async def _retry_from_stage_in_uow(
        self,
        uow: ProductionUnitOfWork,
        run_id: UUID,
        stage: SubjectProductionStage,
        *,
        expected_edition_id: UUID | None = None,
    ) -> SubjectProductionRetryResult:
        """Shared transaction core for manual and automatic business retries."""
        run = await uow.subject_production_runs.get_for_update(run_id)
        if not run:
            raise ProductionRunNotFoundError(str(run_id))
        if expected_edition_id is not None and run.edition_id != expected_edition_id:
            raise ValueError("production_run_edition_changed")

        if run.status in (SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING):
            raise ValueError("retry_not_allowed_while_running")
        if stage not in production_stages():
            raise ValueError("retry_stage_not_in_pipeline")
        if stage is SubjectProductionStage.REFERENCES:
            sources = await uow.source_collections.list_for_subject(run.subject_id)
            source_ready = any(
                source.state.value in {"archived", "extracted", "completed"} for source in sources
            )
            if not source_ready:
                raise ValueError("retry_prerequisite_missing")
        prerequisite = {
            SubjectProductionStage.EXTRACTION: "references",
            SubjectProductionStage.SYNTHESIS: "extraction",
            SubjectProductionStage.ASSEMBLY: "synthesis",
        }.get(stage)
        if prerequisite:
            artifacts = getattr(uow, "production_artifacts", None)
            artifact = (
                await artifacts.get_current(run_id, prerequisite) if artifacts is not None else None
            )
            if artifact is None:
                raise ValueError("retry_prerequisite_missing")

        previous_status = run.status
        previous_stage = run.current_stage
        old_generation = run.pipeline_generation
        run.retry_from_stage(stage, now=datetime.now(UTC))
        artifacts = getattr(uow, "production_artifacts", None)
        staled = (
            await artifacts.mark_from_stage_stale(run_id, stage.value)
            if artifacts is not None
            else []
        )

        await uow.subject_production_runs.save(run)
        return SubjectProductionRetryResult(
            run=run,
            staled_artifacts=staled,
            previous_status=previous_status,
            previous_stage=previous_stage,
            old_generation=old_generation,
        )


_TERMINAL_STATUSES = {
    SubjectProductionStatus.READY,
    SubjectProductionStatus.NEEDS_REVIEW,
    SubjectProductionStatus.FAILED,
    SubjectProductionStatus.CANCELLED,
}


class EditionProductionService:
    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        pacing: ProductionPacingPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._pacing = pacing or ProductionPacingPolicy.zero()

    async def create_batch(
        self,
        edition_id: UUID,
        subject_ids: list[UUID],
        *,
        actor_id: str = "system",
        correlation_id: str = "-",
    ) -> EditionProductionBatch:
        """Create a new production batch for an edition.

        Idempotent: returns existing active batch if one exists.
        """
        async with self._uow_factory() as uow:
            editions = getattr(uow, "editions", None)
            edition = None
            if editions is not None:
                get_for_update = getattr(editions, "get_for_update", None)
                edition = await (
                    get_for_update(edition_id)
                    if get_for_update is not None
                    else editions.get(edition_id)
                )
                if edition is None:
                    raise ValueError("edition_not_found")

            existing = await uow.edition_production_batches.get_active_for_edition(edition_id)
            if existing:
                return existing

            if edition is not None and edition.status is not EditionStatus.SELECTION:
                raise ValueError("edition_must_be_in_selection")

            created_at = datetime.now(UTC)
            before = edition.snapshot() if edition is not None else None
            if edition is not None:
                assert editions is not None
                edition.transition(EditionStatus.PRODUCTION, now=created_at)
                if not await editions.update(edition, edition.version - 1):
                    raise ValueError("edition_concurrent_update")

            batch = EditionProductionBatch(
                edition_id=edition_id,
                status=ProductionBatchStatus.QUEUED,
                phase=ProductionBatchPhase.INITIAL,
                created_at=created_at,
            )
            await uow.edition_production_batches.add(batch)

            if edition is not None:
                await uow.edition_audit.append(
                    EditionAuditEvent(
                        edition_id=edition_id,
                        actor_id=actor_id,
                        action="edition.transitioned",
                        before=before,
                        after=edition.snapshot(),
                        correlation_id=correlation_id,
                        occurred_at=created_at,
                    )
                )

            items = []
            for position, subject_id in enumerate(subject_ids, start=1):
                run = SubjectProductionRun(
                    subject_id=subject_id,
                    edition_id=edition_id,
                    research_date=created_at.date(),
                    created_at=created_at,
                    updated_at=created_at,
                )
                await uow.subject_production_runs.add(run)
                snapshot_repository = getattr(uow, "production_input_snapshots", None)
                if snapshot_repository is not None:
                    snapshot = await capture_production_input_snapshot(
                        uow,
                        production_run_id=run.id,
                        subject_id=subject_id,
                        edition_id=edition_id,
                        research_date=created_at.date(),
                        captured_at=created_at,
                    )
                    await snapshot_repository.add(snapshot)

                item = EditionProductionBatchItem(
                    batch_id=batch.id,
                    subject_id=subject_id,
                    production_run_id=run.id,
                    position=position,
                )
                items.append(item)

            await uow.edition_production_batch_items.append_many(items)
            await uow.commit()
            return batch

    async def get_batch(self, batch_id_or_edition_id: UUID) -> EditionProductionBatch | None:
        """Get a production batch by ID or get active batch for edition."""
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get(batch_id_or_edition_id)
            if batch:
                return batch

            # Fall back to the edition's active batch, then its latest one so
            # the status endpoint keeps working after completion.
            batch = await uow.edition_production_batches.get_active_for_edition(
                batch_id_or_edition_id
            )
            if batch:
                return batch
            return await uow.edition_production_batches.get_latest_for_edition(
                batch_id_or_edition_id
            )

    async def _start_next_in_uow(
        self,
        uow: ProductionUnitOfWork,
        batch: EditionProductionBatch,
        *,
        pace_subject: bool = False,
    ) -> SubjectProductionRun | None:
        """Move the first queued subject of a batch to RUNNING.

        Runs inside the caller's transaction so the batch stays locked for the
        whole decision; the caller commits and dispatches afterwards.
        """
        items = await uow.edition_production_batch_items.list_for_batch(batch.id)
        if batch.status is ProductionBatchStatus.CANCELLED:
            for item in items:
                run = await uow.subject_production_runs.get_for_update(item.production_run_id)
                if run is not None and run.status is SubjectProductionStatus.QUEUED:
                    run.mark_cancelled(now=datetime.now(UTC))
                    await uow.subject_production_runs.save(run)
            return None
        for item in items:
            run = await uow.subject_production_runs.get_for_update(item.production_run_id)
            if run is None:
                continue
            if run.status is SubjectProductionStatus.RUNNING:
                # Already in flight: never dispatch a second job for it.
                return None
            if run.status is SubjectProductionStatus.QUEUED:
                started_at = datetime.now(UTC)
                run.start_running(now=started_at)
                await uow.subject_production_runs.save(run)
                if batch.status is ProductionBatchStatus.QUEUED:
                    batch.start(now=started_at)
                if pace_subject:
                    batch.schedule_next_dispatch(
                        started_at + timedelta(milliseconds=self._pacing.subject_delay_ms())
                    )
                if batch.status is ProductionBatchStatus.RUNNING:
                    await uow.edition_production_batches.save(batch)
                return run
        return None

    async def start_next(self, batch_id: UUID) -> SubjectProductionRun | None:
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get_for_update(batch_id)
            if not batch:
                raise ValueError(f"Batch {batch_id} not found")
            run = await self._start_next_in_uow(uow, batch)
            await uow.commit()
            return run

    async def on_subject_terminal(
        self,
        batch_id: UUID,
        run_id: UUID,
        *,
        actor_id: str = "system",
        correlation_id: str = "-",
    ) -> SubjectProductionRun | None:
        """Hand the batch over after a subject reached a terminal state.

        Returns the run that was started, or None when the batch is finished.
        The caller dispatches the job outside this transaction.
        """
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get_for_update(batch_id)
            if not batch:
                raise ValueError(f"Batch {batch_id} not found")

            started = await self._start_next_in_uow(uow, batch, pace_subject=True)
            if started is not None:
                await uow.commit()
                return started
            if batch.status is ProductionBatchStatus.CANCELLED:
                await uow.commit()
                return None

            items = await uow.edition_production_batch_items.list_for_batch(batch_id)
            all_runs = [
                await uow.subject_production_runs.get(item.production_run_id) for item in items
            ]
            active = next(
                (
                    run
                    for run in all_runs
                    if run is not None and run.status is SubjectProductionStatus.RUNNING
                ),
                None,
            )
            if active is not None:
                await uow.commit()
                return active
            all_terminal = all(
                run is not None and run.status in _TERMINAL_STATUSES for run in all_runs
            )
            if all_terminal and batch.status in {
                ProductionBatchStatus.QUEUED,
                ProductionBatchStatus.RUNNING,
            }:
                recovery = (
                    await self._retry_next_recovery_in_uow(uow, batch, items)
                    if batch.phase is not ProductionBatchPhase.REVIEW
                    else None
                )
                if recovery is not None:
                    await uow.commit()
                    return recovery
                await self._finish_batch_in_uow(
                    uow,
                    batch,
                    items,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                )
            await uow.commit()
            return None

    async def _retry_next_recovery_in_uow(
        self,
        uow: ProductionUnitOfWork,
        batch: EditionProductionBatch,
        items: Sequence[EditionProductionBatchItem],
    ) -> SubjectProductionRun | None:
        """Retry the first eligible terminal item, in editorial order."""
        for item in sorted(items, key=lambda candidate: candidate.position):
            run = await uow.subject_production_runs.get(item.production_run_id)
            if run is None or not ProductionRecoveryPolicyV1.eligible(item, run):
                continue
            try:
                retried = await SubjectProductionService(
                    self._uow_factory
                )._retry_from_stage_in_uow(uow, run.id, run.current_stage)
            except ValueError as exc:
                # A policy-approved error can still lack the prerequisite
                # needed for its stage. It is not an automatic recovery case
                # in that state; leave it for manual review and continue.
                if str(exc) == "retry_prerequisite_missing":
                    continue
                raise
            item.auto_recovery_count += 1
            save_item = getattr(uow.edition_production_batch_items, "save", None)
            if save_item is not None:
                await save_item(item)
            if batch.phase is ProductionBatchPhase.INITIAL:
                batch.enter_recovery()
            batch.schedule_next_dispatch(
                datetime.now(UTC) + timedelta(milliseconds=self._pacing.subject_delay_ms())
            )
            await uow.edition_production_batches.save(batch)
            return retried.run
        return None

    async def _finish_batch_in_uow(
        self,
        uow: ProductionUnitOfWork,
        batch: EditionProductionBatch,
        items: Sequence[EditionProductionBatchItem],
        *,
        actor_id: str,
        correlation_id: str,
    ) -> None:
        runs = [await uow.subject_production_runs.get(item.production_run_id) for item in items]
        has_non_ready = any(
            run is not None and run.status is not SubjectProductionStatus.READY for run in runs
        )
        now = datetime.now(UTC)
        batch.enter_review()
        batch.finish(completed_with_issues=has_non_ready, now=now)
        await uow.edition_production_batches.save(batch)

        editions = getattr(uow, "editions", None)
        audit = getattr(uow, "edition_audit", None)
        if editions is None or audit is None:
            return
        edition = await editions.get_for_update(batch.edition_id)
        if edition is None or edition.status is not EditionStatus.PRODUCTION:
            return
        before = edition.snapshot()
        edition.transition(EditionStatus.REVIEW, now=now)
        update = getattr(editions, "update", None)
        if update is not None and not await update(edition, edition.version - 1):
            raise ValueError("edition_concurrent_update")
        await audit.append(
            EditionAuditEvent(
                edition_id=edition.id,
                actor_id=actor_id,
                action="edition.transitioned",
                before=before,
                after=edition.snapshot(),
                correlation_id=correlation_id,
                occurred_at=now,
            )
        )

    async def clear_next_dispatch(self, run_id: UUID) -> None:
        """Clear the persisted subject schedule when its worker starts."""
        async with self._uow_factory() as uow:
            item = await uow.edition_production_batch_items.get_by_run(run_id)
            if item is None:
                return
            batch = await uow.edition_production_batches.get_for_update(item.batch_id)
            if batch is None:
                return
            batch.clear_next_dispatch()
            await uow.edition_production_batches.save(batch)
            await uow.commit()

    async def next_dispatch_delay_ms(self, batch_id: UUID) -> int:
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get(batch_id)
            return self._pacing.delay_until(batch.next_dispatch_at if batch is not None else None)

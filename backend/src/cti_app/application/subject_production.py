"""Service orchestrating subject production workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from cti_app.application.persistence import (
    ProductionUnitOfWork,
    ProductionUnitOfWorkFactory,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    production_stages,
)


class SubjectProductionService:
    def __init__(self, uow_factory: ProductionUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def create_run(
        self,
        subject_id: UUID,
        edition_id: UUID,
        profile: ProductionProfile,
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

            all_runs = await uow.subject_production_runs.list_for_edition(edition_id)
            subject_runs = [r for r in all_runs if r.subject_id == subject_id]
            next_run_number = len(subject_runs) + 1

            run = SubjectProductionRun(
                subject_id=subject_id,
                edition_id=edition_id,
                profile=profile,
                run_number=next_run_number,
            )
            await uow.subject_production_runs.add(run)
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
    ) -> SubjectProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.mark_needs_review(code=code, message=message, now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def mark_failed(
        self,
        run_id: UUID,
        code: str,
        message: str,
    ) -> SubjectProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.mark_failed(code=code, message=message, now=datetime.now(UTC))
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
    ) -> tuple[SubjectProductionRun, list[str]]:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            if run.status in (SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING):
                raise ValueError("retry_not_allowed_while_running")
            if stage not in production_stages(run.profile):
                raise ValueError("retry_stage_not_in_profile")
            if stage is SubjectProductionStage.REFERENCES:
                sources = await uow.source_collections.list_for_subject(run.subject_id)
                source_ready = any(
                    source.state.value in {"archived", "extracted", "completed"}
                    for source in sources
                )
                if not source_ready:
                    raise ValueError("retry_prerequisite_missing")
            prerequisite = {
                SubjectProductionStage.EXTRACTION: "references",
                SubjectProductionStage.SYNTHESIS: "extraction",
                SubjectProductionStage.ASSEMBLY: "synthesis",
            }.get(stage)
            if prerequisite:
                artifact = await uow.production_artifacts.get_current(run_id, prerequisite)
                if artifact is None:
                    raise ValueError("retry_prerequisite_missing")

            run.retry_from_stage(stage, now=datetime.now(UTC))
            staled = await uow.production_artifacts.mark_from_stage_stale(run_id, stage.value)

            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run, staled


_TERMINAL_STATUSES = {
    SubjectProductionStatus.READY,
    SubjectProductionStatus.NEEDS_REVIEW,
    SubjectProductionStatus.FAILED,
    SubjectProductionStatus.CANCELLED,
}


class EditionProductionService:
    def __init__(self, uow_factory: ProductionUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def create_batch(
        self,
        edition_id: UUID,
        profile: ProductionProfile,
        subject_ids: list[UUID],
    ) -> EditionProductionBatch:
        """Create a new production batch for an edition.

        Idempotent: returns existing active batch if one exists.
        """
        async with self._uow_factory() as uow:
            existing = await uow.edition_production_batches.get_active_for_edition(edition_id)
            if existing and existing.profile == profile:
                return existing

            batch = EditionProductionBatch(
                edition_id=edition_id,
                profile=profile,
                status="queued",
            )
            await uow.edition_production_batches.add(batch)

            items = []
            for position, subject_id in enumerate(subject_ids, start=1):
                run = SubjectProductionRun(
                    subject_id=subject_id,
                    edition_id=edition_id,
                    profile=profile,
                )
                await uow.subject_production_runs.add(run)

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
        self, uow: ProductionUnitOfWork, batch: EditionProductionBatch
    ) -> SubjectProductionRun | None:
        """Move the first queued subject of a batch to RUNNING.

        Runs inside the caller's transaction so the batch stays locked for the
        whole decision; the caller commits and dispatches afterwards.
        """
        items = await uow.edition_production_batch_items.list_for_batch(batch.id)
        for item in items:
            run = await uow.subject_production_runs.get_for_update(item.production_run_id)
            if run is None:
                continue
            if run.status is SubjectProductionStatus.RUNNING:
                # Already in flight: never dispatch a second job for it.
                return None
            if run.status is SubjectProductionStatus.QUEUED:
                run.start_running(now=datetime.now(UTC))
                await uow.subject_production_runs.save(run)
                if batch.status == "queued":
                    batch.start(now=datetime.now(UTC))
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
        self, batch_id: UUID, run_id: UUID
    ) -> SubjectProductionRun | None:
        """Hand the batch over after a subject reached a terminal state.

        Returns the run that was started, or None when the batch is finished.
        The caller dispatches the job outside this transaction.
        """
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get_for_update(batch_id)
            if not batch:
                raise ValueError(f"Batch {batch_id} not found")

            started = await self._start_next_in_uow(uow, batch)
            if started is not None:
                await uow.commit()
                return started

            items = await uow.edition_production_batch_items.list_for_batch(batch_id)
            all_runs = [
                await uow.subject_production_runs.get(item.production_run_id) for item in items
            ]
            all_terminal = all(
                run is not None and run.status in _TERMINAL_STATUSES for run in all_runs
            )
            if all_terminal and batch.status in {"queued", "running"}:
                has_non_ready = any(
                    run is not None and run.status is not SubjectProductionStatus.READY
                    for run in all_runs
                )
                batch.finish(completed_with_issues=has_non_ready, now=datetime.now(UTC))
                await uow.edition_production_batches.save(batch)
            await uow.commit()
            return None

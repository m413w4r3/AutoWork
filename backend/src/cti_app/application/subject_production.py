"""Service orchestrating subject production workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWork
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStatus,
)


class SubjectProductionService:
    """Orchestrates production of a single subject."""

    def __init__(self, uow_factory: ProductionUnitOfWork) -> None:
        self._uow_factory = uow_factory

    async def create_run(
        self,
        subject_id: UUID,
        edition_id: UUID,
        profile: ProductionProfile,
    ) -> SubjectProductionRun:
        """Create a new production run for a subject.

        Idempotent: returns existing active run if one exists.
        """
        async with self._uow_factory() as uow:
            # Check if active run already exists for this subject
            existing = await uow.subject_production_runs.get_current_for_subject(subject_id)
            if existing and existing.status in (
                SubjectProductionStatus.QUEUED,
                SubjectProductionStatus.RUNNING,
            ):
                # Return existing active run instead of creating new one
                return existing

            # Get next run number
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
            return run

    async def start_run(self, run_id: UUID) -> SubjectProductionRun:
        """Start a production run (move from QUEUED to RUNNING)."""
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.start_running(now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def advance_stage(self, run_id: UUID) -> SubjectProductionRun:
        """Advance to next production stage."""
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
        """Mark run as needing human review."""
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
        """Mark run as failed (terminal error)."""
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.mark_failed(code=code, message=message, now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run

    async def cancel_run(self, run_id: UUID) -> SubjectProductionRun:
        """Cancel a production run."""
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            run.mark_cancelled(now=datetime.now(UTC))
            await uow.subject_production_runs.save(run)
            await uow.commit()
            return run


class EditionProductionService:
    """Orchestrates batch production for an entire edition."""

    def __init__(self, uow_factory: ProductionUnitOfWork) -> None:
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
            # Check for existing active batch
            existing = await uow.edition_production_batches.get_active_for_edition(edition_id)
            if existing and existing.profile == profile:
                return existing

            batch = EditionProductionBatch(
                edition_id=edition_id,
                profile=profile,
                status="queued",
            )
            await uow.edition_production_batches.add(batch)

            # Create production runs for each subject and batch items
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
            # Try to get by ID first
            batch = await uow.edition_production_batches.get(batch_id_or_edition_id)
            if batch:
                return batch

            # If not found, try to get active batch for edition
            batch = await uow.edition_production_batches.get_active_for_edition(
                batch_id_or_edition_id
            )
            return batch

    async def create_batch_item_run(
        self,
        batch_id: UUID,
        subject_id: UUID,
        position: int,
    ) -> SubjectProductionRun | None:
        """Create a production run for a subject in a batch.

        Used to create runs for batch items that don't have runs yet.
        """
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get(batch_id)
            if not batch:
                return None

            run = SubjectProductionRun(
                subject_id=subject_id,
                edition_id=batch.edition_id,
                profile=batch.profile,
            )
            await uow.subject_production_runs.add(run)
            await uow.commit()
            return run

    async def start_next(self, batch_id: UUID) -> SubjectProductionRun | None:
        """Start the next subject in a batch (dispatch first item not yet running)."""
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get_for_update(batch_id)
            if not batch:
                raise ValueError(f"Batch {batch_id} not found")

            # Get all items in order
            items = await uow.edition_production_batch_items.list_for_batch(batch_id)
            if not items:
                return None

            # Find first item not in terminal state
            for item in items:
                run = await uow.subject_production_runs.get(item.production_run_id)
                if not run:
                    continue

                # If queued, start it
                if run.status == SubjectProductionStatus.QUEUED:
                    run.start_running(now=datetime.now(UTC))
                    await uow.subject_production_runs.save(run)

                    if batch.status == "queued":
                        batch.start(now=datetime.now(UTC))
                        await uow.edition_production_batches.save(batch)

                    await uow.commit()
                    return run

                # If running, it's already dispatched
                if run.status == SubjectProductionStatus.RUNNING:
                    return run

            return None

    async def on_subject_terminal(self, batch_id: UUID, run_id: UUID) -> bool:
        """Handle subject reaching terminal state (READY/NEEDS_REVIEW/FAILED/CANCELLED).

        Returns True if next item was started, False if batch is complete.
        """
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get_for_update(batch_id)
            if not batch:
                raise ValueError(f"Batch {batch_id} not found")

            # Try to start next subject
            next_run = await self.start_next(batch_id)

            if not next_run:
                # No more subjects to start - check if batch is done
                items = await uow.edition_production_batch_items.list_for_batch(batch_id)
                all_runs = [
                    await uow.subject_production_runs.get(item.production_run_id) for item in items
                ]

                # Check if all are in terminal states
                all_terminal = all(
                    r
                    and r.status
                    in {
                        SubjectProductionStatus.READY,
                        SubjectProductionStatus.NEEDS_REVIEW,
                        SubjectProductionStatus.FAILED,
                        SubjectProductionStatus.CANCELLED,
                    }
                    for r in all_runs
                )

                if all_terminal:
                    # Determine final batch status
                    has_non_ready = any(
                        r and r.status != SubjectProductionStatus.READY for r in all_runs
                    )
                    batch.finish(
                        completed_with_issues=has_non_ready,
                        now=datetime.now(UTC),
                    )
                    await uow.edition_production_batches.save(batch)
                    await uow.commit()

                return False

            return True

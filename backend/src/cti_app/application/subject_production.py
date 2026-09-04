"""Service orchestrating subject production workflow."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from cti_app.application.persistence import (
    ActiveSubjectProductionRunConflictError,
    ProductionUnitOfWork,
    ProductionUnitOfWorkFactory,
)
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.application.production_review_recovery import prepare_batch_for_recovery
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionBatchPhase,
    ProductionBatchStatus,
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionReconciliationRequiredError,
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


@dataclass(frozen=True, slots=True)
class SubjectProductionCancellationResult:
    run: SubjectProductionRun
    batch_id: UUID | None
    changed: bool


class EditionProductionBatchNotFoundError(LookupError):
    pass


class EditionProductionBatchOwnershipError(ValueError):
    pass


class StaleEditionProductionBatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EditionProductionCancellationResult:
    edition: Edition
    batch: EditionProductionBatch
    cancelled_runs: tuple[tuple[UUID, UUID], ...]
    changed: bool


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


async def capture_snapshot_for_new_run(
    uow: ProductionUnitOfWork,
    *,
    run: SubjectProductionRun,
) -> ProductionInputSnapshot:
    """Capture current inputs, preserving the prior research boundary if safe."""
    if run.research_date is None:
        raise ValueError("production_run_research_date_missing")
    current = await capture_production_input_snapshot(
        uow,
        production_run_id=run.id,
        subject_id=run.subject_id,
        edition_id=run.edition_id,
        research_date=run.research_date,
        captured_at=run.created_at,
    )

    get_latest = getattr(
        uow.subject_production_runs, "get_latest_terminal_for_edition_subject", None
    )
    snapshots = uow.production_input_snapshots
    if get_latest is None:
        return current
    previous_run = await get_latest(run.edition_id, run.subject_id)
    if previous_run is None:
        return current
    previous = await snapshots.get_by_run(previous_run.id)
    if previous is None or previous.reuse_basis_hash != current.reuse_basis_hash:
        return current

    run.research_date = previous.research_date
    return await capture_production_input_snapshot(
        uow,
        production_run_id=run.id,
        subject_id=run.subject_id,
        edition_id=run.edition_id,
        research_date=run.research_date,
        captured_at=run.created_at,
    )


class SubjectProductionService:
    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        pacing: ProductionPacingPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._pacing = pacing or ProductionPacingPolicy.zero()

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
        try:
            async with self._uow_factory() as uow:
                lock_creation = getattr(
                    uow.subject_production_runs, "lock_creation_for_subject", None
                )
                if lock_creation is not None:
                    await lock_creation(subject_id)

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
                snapshot = await capture_snapshot_for_new_run(uow, run=run)
                await uow.subject_production_runs.add(run)
                await uow.production_input_snapshots.add(snapshot)
                await uow.commit()
                return run, True
        except ActiveSubjectProductionRunConflictError:
            # The partial unique index is a final race-safety net.  Reload the
            # committed winner so an extremely narrow race remains idempotent.
            async with self._uow_factory() as uow:
                winner = await uow.subject_production_runs.get_current_for_subject(subject_id)
                if winner and winner.status in (
                    SubjectProductionStatus.QUEUED,
                    SubjectProductionStatus.RUNNING,
                ):
                    return winner, False
            raise

    async def start_run(self, run_id: UUID) -> SubjectProductionRun:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ValueError(f"Production run {run_id} not found")

            if run.status is SubjectProductionStatus.CANCELLED:
                await uow.commit()
                return run
            if run.status is not SubjectProductionStatus.RUNNING:
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
        return (await self.cancel_run_with_result(run_id)).run

    async def cancel_run_with_result(self, run_id: UUID) -> SubjectProductionCancellationResult:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get_for_update(run_id)
            if not run:
                raise ProductionRunNotFoundError(str(run_id))

            was_cancelled = run.status is SubjectProductionStatus.CANCELLED
            run.mark_cancelled(now=datetime.now(UTC))
            if not was_cancelled:
                await uow.subject_production_runs.save(run)
            get_by_run = getattr(uow.edition_production_batch_items, "get_by_run", None)
            item = await get_by_run(run_id) if get_by_run is not None else None
            await uow.commit()
            return SubjectProductionCancellationResult(
                run=run,
                batch_id=item.batch_id if item is not None else None,
                changed=not was_cancelled,
            )

    async def retry_from_stage(
        self,
        run_id: UUID,
        stage: SubjectProductionStage,
        *,
        force_recompute: bool = True,
        automatic: bool = False,
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

                # Reject a run that obviously cannot be retried before touching
                # the batch: reopening a finished batch for a cancelled or
                # already running article would be a pure side effect.  The
                # authoritative check stays under the run lock below.
                if initial_run.status is SubjectProductionStatus.CANCELLED:
                    raise ValueError("production_run_cancelled")
                if initial_run.status in (
                    SubjectProductionStatus.QUEUED,
                    SubjectProductionStatus.RUNNING,
                ):
                    raise ValueError("retry_not_allowed_while_running")
                # An unresolved provider submission owns its own recovery use
                # case.  Refuse before the batch is touched so a rejected retry
                # never reopens a finished batch as a side effect; the domain
                # repeats the fence under the run lock.
                if initial_run.requires_reconciliation:
                    raise ProductionReconciliationRequiredError

                item = await uow.edition_production_batch_items.get_by_run(run_id)
                if automatic and (
                    item is None or not ProductionRecoveryPolicyV1.eligible(item, initial_run)
                ):
                    raise ValueError("automatic_recovery_not_allowed")

                # A manifest is immutable evidence of a freeze.  Keep this
                # check under the Edition lock so a retry cannot race with the
                # transaction that creates the manifest.
                manifests = getattr(uow, "publication_manifests", None)
                if (
                    manifests is not None
                    and await manifests.get_latest_for_edition(initial_run.edition_id) is not None
                ):
                    raise ValueError("edition_frozen_for_publication")

                # Edition, then batch, then run: a Review-time retry usually
                # targets a batch that already finished with issues, and the
                # dispatch fences only ever let a dispatchable batch move a
                # subject forward.  This reopens exactly that batch, and
                # refuses cancelled, superseded or busy ones.  A batch still
                # running its initial pass is no exception: one subject at a
                # time is the batch's serialization invariant, and a manual
                # retry must not be the one gesture that breaks it.
                recovery_batch = await prepare_batch_for_recovery(uow, initial_run, reopen=True)
                result = await self._retry_from_stage_in_uow(
                    uow,
                    run_id,
                    stage,
                    expected_edition_id=initial_run.edition_id,
                    force_recompute=force_recompute,
                )
                if automatic and item is not None:
                    item.auto_recovery_count += 1
                    save_item = getattr(uow.edition_production_batch_items, "save", None)
                    if save_item is not None:
                        await save_item(item)
                    if recovery_batch is not None:
                        if recovery_batch.phase is ProductionBatchPhase.INITIAL:
                            recovery_batch.enter_recovery()
                        recovery_batch.schedule_next_dispatch(
                            datetime.now(UTC)
                            + timedelta(
                                milliseconds=self._pacing.subject_delay_ms(
                                    sequence_index=self._pacing.cooldown_every_n_subjects
                                )
                            )
                        )
                        await uow.edition_production_batches.save(recovery_batch)
            else:
                # Lightweight non-database callers predating the Edition port
                # still use the shared transaction core.  The real production
                # UoW always exposes ``editions`` and therefore takes the
                # protected path above.
                result = await self._retry_from_stage_in_uow(
                    uow, run_id, stage, force_recompute=force_recompute
                )
            await uow.commit()
            return result

    async def _retry_from_stage_in_uow(
        self,
        uow: ProductionUnitOfWork,
        run_id: UUID,
        stage: SubjectProductionStage,
        *,
        expected_edition_id: UUID | None = None,
        force_recompute: bool = True,
    ) -> SubjectProductionRetryResult:
        """Shared transaction core for manual and automatic business retries."""
        run = await uow.subject_production_runs.get_for_update(run_id)
        if not run:
            raise ProductionRunNotFoundError(str(run_id))
        if expected_edition_id is not None and run.edition_id != expected_edition_id:
            raise ValueError("production_run_edition_changed")

        if run.status is SubjectProductionStatus.CANCELLED:
            raise ValueError("production_run_cancelled")
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
        run.retry_from_stage(
            stage,
            now=datetime.now(UTC),
            force_recompute=force_recompute,
        )
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
                lock_creation = getattr(
                    uow.subject_production_runs, "lock_creation_for_subject", None
                )
                if lock_creation is not None:
                    await lock_creation(subject_id)
                get_current = getattr(uow.subject_production_runs, "get_current_for_subject", None)
                current_run = await get_current(subject_id) if get_current is not None else None
                if current_run and current_run.status in (
                    SubjectProductionStatus.QUEUED,
                    SubjectProductionStatus.RUNNING,
                ):
                    raise ValueError("subject_production_run_active")
                allocator = getattr(uow.subject_production_runs, "allocate_next_run_number", None)
                if allocator is not None:
                    run_number = await allocator(subject_id)
                else:
                    all_runs = await uow.subject_production_runs.list_for_edition(edition_id)
                    run_number = 1 + sum(1 for item in all_runs if item.subject_id == subject_id)
                run = SubjectProductionRun(
                    subject_id=subject_id,
                    edition_id=edition_id,
                    run_number=run_number,
                    research_date=created_at.date(),
                    created_at=created_at,
                    updated_at=created_at,
                )
                snapshot = await capture_snapshot_for_new_run(uow, run=run)
                await uow.subject_production_runs.add(run)
                await uow.production_input_snapshots.add(snapshot)

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

    async def cancel_batch_with_result(
        self,
        edition_id: UUID,
        batch_id: UUID,
        *,
        actor_id: str = "system",
        correlation_id: str = "-",
    ) -> EditionProductionCancellationResult:
        """Compensate one active batch and return its exact affected runs.

        The owning Edition is the serialization point for production start,
        handoff, and cancellation.  The exact batch is then re-read under
        lock, so an old batch ID cannot cancel a newer active batch.
        """
        async with self._uow_factory() as uow:
            editions = getattr(uow, "editions", None)
            if editions is None:
                raise ValueError("edition_repository_missing")

            edition = await editions.get_for_update(edition_id)
            if edition is None:
                raise EditionProductionBatchNotFoundError(str(edition_id))

            batch = await uow.edition_production_batches.get_for_update(batch_id)
            if batch is None:
                raise EditionProductionBatchNotFoundError(str(batch_id))
            if batch.edition_id != edition_id:
                raise EditionProductionBatchOwnershipError(
                    "Production batch does not belong to this edition"
                )

            active = await uow.edition_production_batches.get_active_for_edition(edition_id)
            if active is not None and active.id != batch.id:
                raise StaleEditionProductionBatchError(
                    "A newer production batch is active for this edition"
                )

            # A repeated request for the same current cancelled batch is an
            # idempotent success.  It must not repair or mutate anything else,
            # especially not a later batch.
            if batch.status is ProductionBatchStatus.CANCELLED:
                await uow.commit()
                return EditionProductionCancellationResult(
                    edition=edition,
                    batch=batch,
                    cancelled_runs=(),
                    changed=False,
                )

            # Check terminal status before the Edition status so a completed
            # batch always returns its typed business conflict, even if an
            # older inconsistent record still says production.
            if batch.status in {
                ProductionBatchStatus.COMPLETED,
                ProductionBatchStatus.COMPLETED_WITH_ISSUES,
            }:
                batch.cancel()

            if edition.status is not EditionStatus.PRODUCTION:
                raise ValueError("edition_not_in_production")

            # The domain operation rejects completed terminal batches and is
            # deliberately not represented by Edition.transition.
            now = datetime.now(UTC)
            batch.cancel(now=now)
            await uow.edition_production_batches.save(batch)

            cancelled_runs: list[tuple[UUID, UUID]] = []
            items = await uow.edition_production_batch_items.list_for_batch(batch.id)
            for item in items:
                run = await uow.subject_production_runs.get_for_update(item.production_run_id)
                if run is None:
                    raise ValueError("production_batch_run_missing")
                if run.edition_id != edition_id or run.subject_id != item.subject_id:
                    raise EditionProductionBatchOwnershipError(
                        "Production run does not belong to this batch"
                    )
                if run.status in {
                    SubjectProductionStatus.QUEUED,
                    SubjectProductionStatus.RUNNING,
                }:
                    run.mark_cancelled(now=now)
                    await uow.subject_production_runs.save(run)
                    cancelled_runs.append((run.id, run.subject_id))

            before = edition.snapshot()
            edition.return_to_selection_after_production_cancellation(now=now)
            if not await editions.update(edition, edition.version - 1):
                raise ValueError("edition_concurrent_update")
            await uow.edition_audit.append(
                EditionAuditEvent(
                    edition_id=edition.id,
                    actor_id=actor_id,
                    action="edition.production_cancelled",
                    before=before,
                    after=edition.snapshot(),
                    correlation_id=correlation_id,
                    occurred_at=now,
                )
            )
            await uow.commit()
            return EditionProductionCancellationResult(
                edition=edition,
                batch=batch,
                cancelled_runs=tuple(cancelled_runs),
                changed=True,
            )

    async def cancel_batch(
        self,
        edition_id: UUID,
        batch_id: UUID,
        *,
        actor_id: str = "system",
        correlation_id: str = "-",
    ) -> EditionProductionCancellationResult:
        """Alias for the explicit production cancellation use case."""
        return await self.cancel_batch_with_result(
            edition_id,
            batch_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )

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

    async def _get_batch_for_update_in_lock_order(
        self,
        uow: ProductionUnitOfWork,
        batch_id: UUID,
    ) -> EditionProductionBatch:
        """Acquire Edition then batch, matching production cancellation."""
        probe = await uow.edition_production_batches.get(batch_id)
        if probe is None:
            raise ValueError(f"Batch {batch_id} not found")

        editions = getattr(uow, "editions", None)
        if editions is not None:
            edition = await editions.get_for_update(probe.edition_id)
            if edition is None:
                raise ValueError("edition_not_found")

        batch = await uow.edition_production_batches.get_for_update(batch_id)
        if batch is None:
            raise ValueError(f"Batch {batch_id} not found")
        return batch

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
                        started_at
                        + timedelta(
                            milliseconds=self._pacing.subject_delay_ms(sequence_index=item.position)
                        )
                    )
                if batch.status is ProductionBatchStatus.RUNNING:
                    await uow.edition_production_batches.save(batch)
                return run
        return None

    async def start_next(self, batch_id: UUID) -> SubjectProductionRun | None:
        async with self._uow_factory() as uow:
            batch = await self._get_batch_for_update_in_lock_order(uow, batch_id)
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
            batch = await self._get_batch_for_update_in_lock_order(uow, batch_id)

            item_for_run = await uow.edition_production_batch_items.get_by_run(run_id)
            if item_for_run is None or item_for_run.batch_id != batch.id:
                await uow.commit()
                return None

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
                )._retry_from_stage_in_uow(uow, run.id, run.current_stage, force_recompute=False)
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
            # Une reprise automatique suit toujours un échec : elle mérite le
            # palier de repos long, jamais le simple jitter.
            batch.schedule_next_dispatch(
                datetime.now(UTC)
                + timedelta(
                    milliseconds=self._pacing.subject_delay_ms(
                        sequence_index=self._pacing.cooldown_every_n_subjects
                    )
                )
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
            batch = await self._get_batch_for_update_in_lock_order(uow, item.batch_id)
            batch.clear_next_dispatch()
            await uow.edition_production_batches.save(batch)
            await uow.commit()

    async def next_dispatch_delay_ms(self, batch_id: UUID) -> int:
        async with self._uow_factory() as uow:
            batch = await uow.edition_production_batches.get(batch_id)
            return self._pacing.delay_until(batch.next_dispatch_at if batch is not None else None)

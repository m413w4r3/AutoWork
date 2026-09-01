"""One business rule for resuming a single article from Review.

Two operator gestures resume an article that stopped during production: a
deliberate retry, and the adoption of a reconciled ChatGPT answer.  Both reach
the same state of the world — the batch that owns the article has usually
already finished as ``completed_with_issues`` while the edition sits in
``review`` — and both must obey the same invariants.  Keeping the decision in
one place is what stops the two paths from drifting into a collection of
special cases spread over jobs, services and the API.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from cti_app.domain.production import (
    EditionProductionBatch,
    ProductionBatchRecoveryConflictError,
    ProductionBatchStatus,
    SubjectProductionRun,
    SubjectProductionStatus,
)

# Reasons are stable business identities: callers map them to their own typed
# error vocabulary instead of parsing a message.
BATCH_MISSING = "batch_missing"
BATCH_CANCELLED = "batch_cancelled"
BATCH_SUPERSEDED = "batch_superseded"
ACTIVE_SIBLING = "active_sibling"


class ReviewRecoveryConflictError(ValueError):
    """A typed refusal to resume one article of a production batch."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"production_{reason}")


async def prepare_batch_for_recovery(
    uow: Any,
    run: SubjectProductionRun,
    *,
    reopen: bool,
    require_idle_siblings: bool = True,
) -> EditionProductionBatch | None:
    """Validate — and optionally perform — the batch side of a resume.

    The caller must already hold the Edition lock, and must not yet hold the
    run lock: the lock order is Edition, then batch, then run, exactly as the
    batch hand-off and the production cancellation use case take them.

    ``reopen`` distinguishes the read-only precondition check from the
    transition itself, so a preview or a validation pass never mutates a
    batch.  Returns the batch, or ``None`` when the run does not belong to one.
    """
    items = getattr(uow, "edition_production_batch_items", None)
    batches = getattr(uow, "edition_production_batches", None)
    if items is None or batches is None:
        return None
    get_by_run = getattr(items, "get_by_run", None)
    item = await get_by_run(run.id) if get_by_run is not None else None
    if item is None:
        return None

    batch = await _get_batch(batches, item.batch_id, for_update=reopen)
    if batch is None:
        raise ReviewRecoveryConflictError(BATCH_MISSING)
    if batch.status is ProductionBatchStatus.CANCELLED:
        raise ReviewRecoveryConflictError(BATCH_CANCELLED)

    needs_reopening = batch.status not in {
        ProductionBatchStatus.QUEUED,
        ProductionBatchStatus.RUNNING,
    }
    if needs_reopening:
        # A finished batch is only ever reopened while it is still the one the
        # edition is reviewing.  An operator acting on a stale Review view must
        # never resurrect an old batch behind a newer production.
        await _ensure_not_superseded(batches, batch)

    if require_idle_siblings or needs_reopening:
        await _ensure_no_active_sibling(uow, items, batch.id, run.id)

    if reopen and needs_reopening:
        batch.open_review_recovery()
        save = getattr(batches, "save", None)
        if save is not None:
            await save(batch)
    return batch


async def _get_batch(
    batches: Any, batch_id: UUID, *, for_update: bool
) -> EditionProductionBatch | None:
    if for_update:
        get_for_update = getattr(batches, "get_for_update", None)
        if get_for_update is not None:
            result: EditionProductionBatch | None = await get_for_update(batch_id)
            return result
    get = getattr(batches, "get", None) or getattr(batches, "get_for_update", None)
    if get is None:
        return None
    plain: EditionProductionBatch | None = await get(batch_id)
    return plain


async def _ensure_not_superseded(batches: Any, batch: EditionProductionBatch) -> None:
    get_active = getattr(batches, "get_active_for_edition", None)
    if get_active is None:
        return
    active = await get_active(batch.edition_id)
    if active is not None and active.id != batch.id:
        raise ReviewRecoveryConflictError(BATCH_SUPERSEDED)


async def _ensure_no_active_sibling(uow: Any, items: Any, batch_id: UUID, run_id: UUID) -> None:
    list_for_batch = getattr(items, "list_for_batch", None)
    if list_for_batch is None:
        return
    for sibling in await list_for_batch(batch_id):
        if sibling.production_run_id == run_id:
            continue
        sibling_run = await uow.subject_production_runs.get(sibling.production_run_id)
        if sibling_run is not None and sibling_run.status is SubjectProductionStatus.RUNNING:
            raise ReviewRecoveryConflictError(ACTIVE_SIBLING)


__all__ = [
    "ACTIVE_SIBLING",
    "BATCH_CANCELLED",
    "BATCH_MISSING",
    "BATCH_SUPERSEDED",
    "ProductionBatchRecoveryConflictError",
    "ReviewRecoveryConflictError",
    "prepare_batch_for_recovery",
]

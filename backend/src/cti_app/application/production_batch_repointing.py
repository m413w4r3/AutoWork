"""Shared helpers for keeping edition review attached to the current run."""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def _repoint_batch_item(
    uow: Any,
    replaced_run_id: UUID | None,
    replacement_run_id: UUID,
) -> None:
    """Point the edition batch item at the run that replaces an old one.

    A subject produced outside an edition batch has no item; that is a normal
    case and not an error. The Review read model joins on this exact run id.
    """
    if replaced_run_id is None:
        return
    items = getattr(uow, "edition_production_batch_items", None)
    if items is None:
        return
    get_by_run = getattr(items, "get_by_run", None)
    save = getattr(items, "save", None)
    if get_by_run is None or save is None:
        return
    item = await get_by_run(replaced_run_id)
    if item is None:
        return
    item.production_run_id = replacement_run_id
    # A manual analyst restart must not consume the single automatic retry
    # still owed by the batch.
    item.auto_recovery_count = 0
    await save(item)

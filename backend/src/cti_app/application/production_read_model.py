"""Read models used by the production UI.

These DTOs deliberately do not replace the transactional production
repositories.  They contain only the denormalized data needed to render a
batch status list efficiently.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class BatchStatusItem:
    """One row in the UI-facing production batch status read model."""

    position: int
    subject_id: UUID
    title: str
    run_id: UUID
    status: str
    current_stage: str
    pipeline_generation: int
    auto_recovery_count: int
    error_code: str | None
    error_message: str | None


class BatchStatusReadRepository(Protocol):
    """Port for the optimized batch status projection."""

    async def list_for_batch(self, batch_id: UUID) -> Sequence[BatchStatusItem]: ...


class BatchStatusReadService:
    """Application service for reading the production batch projection."""

    def __init__(self, repository: BatchStatusReadRepository) -> None:
        self._repository = repository

    async def list_items(self, batch_id: UUID) -> Sequence[BatchStatusItem]:
        return await self._repository.list_for_batch(batch_id)

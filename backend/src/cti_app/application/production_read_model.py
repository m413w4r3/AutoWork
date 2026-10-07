"""Read models used by the production UI.

These DTOs deliberately do not replace the transactional production
repositories.  They contain only the denormalized data needed to render a
batch status list efficiently.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from cti_app.domain.production import (
    ProductionRunStatus,
    ProductionStage,
    ProductionSubmissionReconciliation,
)


@dataclass(frozen=True, slots=True)
class BatchStatusItem:
    """One row in the UI-facing production batch status read model."""

    position: int
    subject_id: UUID
    title: str
    run_id: UUID
    status: ProductionRunStatus
    current_stage: ProductionStage
    pipeline_generation: int
    auto_recovery_count: int
    error_code: str | None
    error_message: str | None
    extraction_progress: dict[str, Any] | None = None
    reconciliation: ProductionSubmissionReconciliation | None = None


@dataclass(frozen=True, slots=True)
class ProductionActivitySnapshot:
    """Canonical job/model-run state used to explain one subject's activity."""

    subject_id: UUID
    run_id: UUID
    stage: ProductionStage
    run_status: ProductionRunStatus
    job_kind: str | None = None
    job_status: str | None = None
    job_created_at: datetime | None = None
    job_started_at: datetime | None = None
    next_retry_at: datetime | None = None
    attempt: int | None = None
    model_run_status: str | None = None
    model_run_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProductionActivity:
    kind: str
    stage: ProductionStage | None
    started_at: datetime | None
    since_seconds: int | None
    detail: str | None
    attempt: int | None = None


def derive_production_activity(
    snapshot: ProductionActivitySnapshot | None,
    *,
    now: datetime | None = None,
) -> ProductionActivity:
    """Describe canonical work without treating queued probes as model calls."""
    if snapshot is None:
        return ProductionActivity("idle", None, None, None, None)

    current = now or datetime.now(UTC)
    kind = snapshot.job_kind
    status = snapshot.job_status
    started_at: datetime | None = None
    activity_kind = "idle"
    detail: str | None = None

    if kind == "production.subject.reconciliation_probe" and status in {"queued", "running"}:
        activity_kind = "reconciliation_probe"
        started_at = snapshot.job_started_at or snapshot.job_created_at
        detail = "Sondage de réconciliation · aucune requête modèle"
    elif status == "queued" and snapshot.next_retry_at is not None:
        activity_kind = "retry_scheduled"
        started_at = snapshot.next_retry_at
        detail = "Nouvel essai programmé"
    elif (
        status == "running"
        and snapshot.model_run_status == "running"
        and snapshot.model_run_started_at is not None
    ):
        activity_kind = "model_call"
        started_at = snapshot.model_run_started_at
        detail = "Appel modèle en cours"
    elif status == "running":
        activity_kind = "deterministic_stage"
        started_at = snapshot.job_started_at or snapshot.job_created_at
        detail = "Étape en cours"
    elif snapshot.run_status is ProductionRunStatus.QUEUED or status == "queued":
        activity_kind = "waiting_batch"
        started_at = snapshot.job_created_at
        detail = "En attente du prochain article"

    since_seconds = None
    if started_at is not None:
        normalized = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=UTC)
        normalized_now = current if current.tzinfo is not None else current.replace(tzinfo=UTC)
        since_seconds = max(0, int((normalized_now - normalized).total_seconds()))
    return ProductionActivity(
        kind=activity_kind,
        stage=snapshot.stage,
        started_at=started_at,
        since_seconds=since_seconds,
        detail=detail,
        attempt=snapshot.attempt,
    )


@dataclass(frozen=True, slots=True)
class ProductionRunSummary:
    run_id: UUID
    edition_id: UUID
    subject_id: UUID
    run_number: int
    status: ProductionRunStatus
    current_stage: ProductionStage
    pipeline_generation: int
    research_date: Any
    created_at: Any
    started_at: Any
    finished_at: Any
    error_code: str | None
    error_message: str | None


class BatchStatusReadRepository(Protocol):
    """Port for the optimized batch status projection."""

    async def list_for_batch(self, batch_id: UUID) -> Sequence[BatchStatusItem]: ...

    async def list_activity_for_runs(
        self, run_ids: Sequence[UUID]
    ) -> Sequence[ProductionActivitySnapshot]: ...


class BatchStatusReadService:
    """Application service for reading the production batch projection."""

    def __init__(self, repository: BatchStatusReadRepository) -> None:
        self._repository = repository

    async def list_items(self, batch_id: UUID) -> Sequence[BatchStatusItem]:
        return await self._repository.list_for_batch(batch_id)

"""Domain models for subject production workflow (brief_auto)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ProductionProfile(StrEnum):
    BRIEF_AUTO = "brief_auto"
    MAJOR_ASSISTED = "major_assisted"


class SubjectProductionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubjectProductionStage(StrEnum):
    SOURCES = "sources"
    REFERENCES = "references"
    EXTRACTION = "extraction"
    SYNTHESIS = "synthesis"
    ASSEMBLY = "assembly"


class ProductionArtifactStage(StrEnum):
    REFERENCES = "references"
    EXTRACTION = "extraction"
    SYNTHESIS = "synthesis"
    BRIEF = "brief"


class ProductionArtifactStatus(StrEnum):
    VERIFIED = "verified"
    STALE = "stale"
    NEEDS_REVIEW = "needs_review"


@dataclass(slots=True, kw_only=True)
class SubjectProductionRun:
    subject_id: UUID
    edition_id: UUID
    profile: ProductionProfile
    status: SubjectProductionStatus = SubjectProductionStatus.QUEUED
    current_stage: SubjectProductionStage = SubjectProductionStage.SOURCES
    conversation_id: UUID | None = None
    run_number: int = 1
    # Frozen when the run starts: a retry after midnight must not shift the
    # boundary used to reject impossible publication dates.
    research_date: date | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: int = 1

    def __post_init__(self) -> None:
        if self.run_number < 1 or self.version < 1:
            raise ValueError("run_number and version must be >= 1")

    def start_running(self, *, now: datetime | None = None) -> None:
        if self.status is not SubjectProductionStatus.QUEUED:
            raise ValueError("Can only start from QUEUED status")
        self.status = SubjectProductionStatus.RUNNING
        self.started_at = now or datetime.now(UTC)
        if self.research_date is None:
            self.research_date = self.started_at.date()
        self.updated_at = self.started_at
        self.version += 1

    def advance_stage(self, *, now: datetime | None = None) -> None:
        stages = list(SubjectProductionStage)
        current_idx = stages.index(self.current_stage)
        if current_idx < len(stages) - 1:
            self.current_stage = stages[current_idx + 1]
        self.updated_at = now or datetime.now(UTC)
        self.version += 1

    def mark_ready(self, *, now: datetime | None = None) -> None:
        """READY implies assembly complete and QA passed."""
        if self.status is SubjectProductionStatus.READY:
            return
        self.status = SubjectProductionStatus.READY
        self.finished_at = now or datetime.now(UTC)
        self.updated_at = self.finished_at
        self.version += 1

    def mark_needs_review(
        self,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.status = SubjectProductionStatus.NEEDS_REVIEW
        self.error_code = code[:64]
        self.error_message = " ".join(message.replace("\x00", "").split())[:500]
        self.error_details = details
        self.finished_at = now or datetime.now(UTC)
        self.updated_at = self.finished_at
        self.version += 1

    def mark_failed(
        self,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.status = SubjectProductionStatus.FAILED
        self.error_code = code[:64]
        self.error_message = " ".join(message.replace("\x00", "").split())[:500]
        self.error_details = details
        self.finished_at = now or datetime.now(UTC)
        self.updated_at = self.finished_at
        self.version += 1

    def mark_cancelled(self, *, now: datetime | None = None) -> None:
        self.status = SubjectProductionStatus.CANCELLED
        self.finished_at = now or datetime.now(UTC)
        self.updated_at = self.finished_at
        self.version += 1


@dataclass(slots=True, kw_only=True)
class ProductionArtifact:
    """Immutable artifact produced during a production stage."""

    production_run_id: UUID
    subject_id: UUID
    stage: ProductionArtifactStage
    version: int
    input_hash: str  # SHA-256
    status: ProductionArtifactStatus = ProductionArtifactStatus.VERIFIED
    raw_blob_id: UUID | None = None
    canonical_blob_id: UUID | None = None
    rendered_blob_id: UUID | None = None
    model_run_id: UUID | None = None
    conversation_turn_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if len(self.input_hash) != 64 or any(c not in "0123456789abcdef" for c in self.input_hash):
            raise ValueError("input_hash must be lowercase SHA-256")


@dataclass(slots=True, kw_only=True)
class EditionProductionBatch:
    edition_id: UUID
    profile: ProductionProfile
    status: str  # queued, running, completed, completed_with_issues, cancelled
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    version: int = 1

    def __post_init__(self) -> None:
        valid_statuses = {
            "queued",
            "running",
            "completed",
            "completed_with_issues",
            "cancelled",
        }
        if self.status not in valid_statuses:
            raise ValueError(f"Invalid status: {self.status}")

    def start(self, *, now: datetime | None = None) -> None:
        if self.status != "queued":
            raise ValueError("Can only start from queued status")
        self.status = "running"
        self.started_at = now or datetime.now(UTC)
        self.version += 1

    def finish(self, *, completed_with_issues: bool = False, now: datetime | None = None) -> None:
        if self.status != "running":
            raise ValueError("Can only finish from running status")
        self.status = "completed_with_issues" if completed_with_issues else "completed"
        self.finished_at = now or datetime.now(UTC)
        self.version += 1

    def cancel(self, *, now: datetime | None = None) -> None:
        self.status = "cancelled"
        self.finished_at = now or datetime.now(UTC)
        self.version += 1


@dataclass(slots=True, kw_only=True)
class EditionProductionBatchItem:
    batch_id: UUID
    subject_id: UUID
    production_run_id: UUID
    position: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.position < 1:
            raise ValueError("position must be >= 1")

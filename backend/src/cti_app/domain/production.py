"""Domain models for subject production workflows."""

from __future__ import annotations

import re
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
    ANALYST_RESEARCH = "analyst_research"
    ANALYST_NOTE = "analyst_note"
    ASSEMBLY = "assembly"


def production_stages(profile: ProductionProfile) -> tuple[SubjectProductionStage, ...]:
    """The ordered, executable-or-manual production pipeline for a profile."""
    pipelines: dict[ProductionProfile, tuple[SubjectProductionStage, ...]] = {
        ProductionProfile.BRIEF_AUTO: (
            SubjectProductionStage.SOURCES,
            SubjectProductionStage.REFERENCES,
            SubjectProductionStage.EXTRACTION,
            SubjectProductionStage.SYNTHESIS,
            SubjectProductionStage.ASSEMBLY,
        ),
        ProductionProfile.MAJOR_ASSISTED: (
            SubjectProductionStage.SOURCES,
            SubjectProductionStage.REFERENCES,
            SubjectProductionStage.EXTRACTION,
            SubjectProductionStage.SYNTHESIS,
            SubjectProductionStage.ANALYST_RESEARCH,
            SubjectProductionStage.ANALYST_NOTE,
            SubjectProductionStage.ASSEMBLY,
        ),
    }
    return pipelines[profile]


def next_stage(
    profile: ProductionProfile, stage: SubjectProductionStage
) -> SubjectProductionStage | None:
    """Return the declared successor for a profile; enum declaration order is irrelevant."""
    stages = production_stages(profile)
    try:
        index = stages.index(stage)
    except ValueError as exc:
        raise ValueError(f"Stage {stage.value} is not valid for {profile.value}") from exc
    return stages[index + 1] if index + 1 < len(stages) else None


class ProductionArtifactStage(StrEnum):
    REFERENCES = "references"
    EXTRACTION = "extraction"
    SYNTHESIS = "synthesis"
    BRIEF = "brief"


class ProductionArtifactStatus(StrEnum):
    VERIFIED = "verified"
    STALE = "stale"
    NEEDS_REVIEW = "needs_review"


class AnalystInvestigationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AnalystInvestigationStage(StrEnum):
    SEEDS = "seeds"
    FEATURES = "features"
    TOOLING = "tooling"
    INVARIANTS = "invariants"
    PIVOTS = "pivots"
    CORPUS = "corpus"
    DETECTION = "detection"
    NOTE = "note"


class LoopBudgetCategory(StrEnum):
    PIVOT_RUNS = "pivot_runs"
    HITS_ACQUIRED = "hits_acquired"
    NEW_SAMPLES = "new_samples"
    VT_READ_UNITS = "vt_read_units"


@dataclass(slots=True, kw_only=True)
class LoopBudget:
    max_cycles: int = 3
    max_pivot_runs: int = 0
    max_hits_acquired: int = 0
    max_new_samples: int = 0
    max_vt_read_units: int = 0
    consumed_pivot_runs: int = 0
    consumed_hits_acquired: int = 0
    consumed_new_samples: int = 0
    consumed_vt_read_units: int = 0

    def __post_init__(self) -> None:
        values = (
            self.max_cycles,
            self.max_pivot_runs,
            self.max_hits_acquired,
            self.max_new_samples,
            self.max_vt_read_units,
            self.consumed_pivot_runs,
            self.consumed_hits_acquired,
            self.consumed_new_samples,
            self.consumed_vt_read_units,
        )
        if any(value < 0 for value in values) or self.max_cycles < 1:
            raise ValueError("Budget limits must be non-negative and max_cycles must be >= 1")

    def consume(self, category: LoopBudgetCategory, units: int = 1) -> None:
        fields = {
            LoopBudgetCategory.PIVOT_RUNS: ("max_pivot_runs", "consumed_pivot_runs"),
            LoopBudgetCategory.HITS_ACQUIRED: ("max_hits_acquired", "consumed_hits_acquired"),
            LoopBudgetCategory.NEW_SAMPLES: ("max_new_samples", "consumed_new_samples"),
            LoopBudgetCategory.VT_READ_UNITS: ("max_vt_read_units", "consumed_vt_read_units"),
        }
        if units < 1:
            raise ValueError("Budget consumption must be positive")
        try:
            maximum_name, consumed_name = fields[category]
        except KeyError as exc:
            raise ValueError(f"Unknown budget category: {category}") from exc
        if getattr(self, consumed_name) + units > getattr(self, maximum_name):
            raise ValueError(f"Budget exceeded for {category}")
        setattr(self, consumed_name, getattr(self, consumed_name) + units)


@dataclass(slots=True, kw_only=True)
class SubjectProductionRun:
    subject_id: UUID
    edition_id: UUID
    profile: ProductionProfile
    status: SubjectProductionStatus = SubjectProductionStatus.QUEUED
    current_stage: SubjectProductionStage = SubjectProductionStage.SOURCES
    references_conversation_id: UUID | None = None
    synthesis_conversation_id: UUID | None = None
    run_number: int = 1
    # A manual retry is a new pipeline generation, distinct from a worker's
    # technical attempts.  It scopes every side effect identity in the chain.
    pipeline_generation: int = 0
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
        if self.pipeline_generation < 0:
            raise ValueError("pipeline_generation must be >= 0")

    def start_running(self, *, now: datetime | None = None) -> None:
        if self.status is not SubjectProductionStatus.QUEUED:
            raise ValueError("Can only start from QUEUED status")
        self.status = SubjectProductionStatus.RUNNING
        self.started_at = now or datetime.now(UTC)
        if self.research_date is None:
            self.research_date = self.started_at.date()
        self.updated_at = self.started_at
        self.version += 1

    def resume_verified_import_at_analyst_research(self, *, now: datetime | None = None) -> None:
        """Resume imported verified artifacts at the major manual checkpoint."""
        if self.profile is not ProductionProfile.MAJOR_ASSISTED:
            raise ValueError("Only major_assisted runs have an analyst checkpoint")
        if self.status is not SubjectProductionStatus.QUEUED:
            raise ValueError("Imported run must start from QUEUED status")
        if self.current_stage is not SubjectProductionStage.SOURCES:
            raise ValueError("Imported run must start from SOURCES stage")
        if self.research_date is None:
            raise ValueError("Analyst handoff requires frozen research_date")
        moment = now or datetime.now(UTC)
        self.status = SubjectProductionStatus.RUNNING
        self.current_stage = SubjectProductionStage.ANALYST_RESEARCH
        self.started_at = moment
        self.finished_at = None
        self.error_code = None
        self.error_message = None
        self.error_details = None
        self.updated_at = moment
        self.version += 1

    def advance_stage(self, *, now: datetime | None = None) -> None:
        successor = next_stage(self.profile, self.current_stage)
        if successor is not None:
            self.current_stage = successor
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

    def retry_from_stage(
        self, stage: SubjectProductionStage, *, now: datetime | None = None
    ) -> None:
        """Start a deliberate new pipeline generation at ``stage``.

        Unlike a worker retry, this invalidates the selected stage and its
        downstream outputs.  Callers must validate prerequisites first.
        """
        if self.status in (SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING):
            raise ValueError("Cannot retry a queued or running production")
        self.status = SubjectProductionStatus.RUNNING
        self.current_stage = stage
        self.pipeline_generation += 1
        self.error_code = None
        self.error_message = None
        self.error_details = None
        self.finished_at = None
        self.updated_at = now or datetime.now(UTC)
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
class AnalystInvestigation:
    production_run_id: UUID
    subject_id: UUID
    synthesis_artifact_id: UUID
    budget: LoopBudget
    status: AnalystInvestigationStatus = AnalystInvestigationStatus.QUEUED
    current_stage: AnalystInvestigationStage = AnalystInvestigationStage.SEEDS
    cycle_number: int = 1
    input_pack_blob_id: UUID | None = None
    input_sha256: str | None = None
    pivot_conversation_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: int = 1

    def __post_init__(self) -> None:
        if self.version < 1 or self.cycle_number < 1:
            raise ValueError("version and cycle_number must be >= 1")
        if (self.input_pack_blob_id is None) != (self.input_sha256 is None):
            raise ValueError("input pack blob and SHA-256 must be supplied together")
        if self.input_sha256 and (
            len(self.input_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.input_sha256)
        ):
            raise ValueError("input_sha256 must be lowercase SHA-256")

    @classmethod
    def from_verified_synthesis(
        cls, *, synthesis: ProductionArtifact, budget: LoopBudget, **kwargs: Any
    ) -> AnalystInvestigation:
        if (
            synthesis.stage is not ProductionArtifactStage.SYNTHESIS
            or synthesis.status is not ProductionArtifactStatus.VERIFIED
        ):
            raise ValueError("Analyst investigation requires a verified SYNTHESIS artifact")
        if (
            kwargs.get("production_run_id", synthesis.production_run_id)
            != synthesis.production_run_id
        ):
            raise ValueError("Synthesis artifact must belong to the production run")
        if kwargs.get("subject_id", synthesis.subject_id) != synthesis.subject_id:
            raise ValueError("Synthesis artifact must belong to the subject")
        return cls(
            production_run_id=synthesis.production_run_id,
            subject_id=synthesis.subject_id,
            synthesis_artifact_id=synthesis.id,
            budget=budget,
            **kwargs,
        )

    def start(self, *, now: datetime | None = None) -> None:
        if self.status is not AnalystInvestigationStatus.QUEUED:
            raise ValueError("Can only start a queued investigation")
        self.status = AnalystInvestigationStatus.RUNNING
        self.started_at = now or datetime.now(UTC)
        self._bump(self.started_at)

    def advance_stage(self, *, now: datetime | None = None) -> None:
        stages = tuple(AnalystInvestigationStage)
        index = stages.index(self.current_stage)
        if index + 1 < len(stages):
            self.current_stage = stages[index + 1]
        self._bump(now)

    def consume_budget(
        self,
        category: LoopBudgetCategory,
        units: int = 1,
        *,
        now: datetime | None = None,
    ) -> None:
        self.budget.consume(category, units)
        self._bump(now)

    def finish_cycle(self, *, validated_new_members: int, now: datetime | None = None) -> None:
        if self.status is not AnalystInvestigationStatus.RUNNING:
            raise ValueError("Only a running investigation can finish a cycle")
        if validated_new_members < 0:
            raise ValueError("validated_new_members must be non-negative")
        moment = now or datetime.now(UTC)
        if validated_new_members == 0 or self.cycle_number >= self.budget.max_cycles:
            self.status = AnalystInvestigationStatus.EXHAUSTED
            self.finished_at = moment
        else:
            self.cycle_number += 1
        self._bump(moment)

    def await_review(self, *, now: datetime | None = None) -> None:
        if self.status is not AnalystInvestigationStatus.RUNNING:
            raise ValueError("Only a running investigation can await review")
        self.status = AnalystInvestigationStatus.AWAITING_REVIEW
        self._bump(now)

    def complete(self, *, now: datetime | None = None) -> None:
        if self.status not in (
            AnalystInvestigationStatus.RUNNING,
            AnalystInvestigationStatus.AWAITING_REVIEW,
        ):
            raise ValueError("Only an active investigation can complete")
        self.status = AnalystInvestigationStatus.COMPLETED
        self.finished_at = now or datetime.now(UTC)
        self._bump(self.finished_at)

    def fail_technical(self, *, now: datetime | None = None) -> None:
        if self.status not in (
            AnalystInvestigationStatus.RUNNING,
            AnalystInvestigationStatus.AWAITING_REVIEW,
        ):
            raise ValueError("Only an active investigation can fail")
        self.status = AnalystInvestigationStatus.FAILED
        self.finished_at = now or datetime.now(UTC)
        self._bump(self.finished_at)

    def cancel(self, *, now: datetime | None = None) -> None:
        self.status = AnalystInvestigationStatus.CANCELLED
        self.finished_at = now or datetime.now(UTC)
        self._bump(self.finished_at)

    def _bump(self, now: datetime | None = None) -> None:
        self.updated_at = now or datetime.now(UTC)
        self.version += 1


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalystInputPack:
    investigation_id: UUID
    blob_id: UUID
    sha256: str
    schema_version: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise ValueError("sha256 must be lowercase SHA-256")


class SampleAcquisitionReason(StrEnum):
    """Why bytes were pulled from VirusTotal for the investigation.

    `HIT_REVIEW` is reachable from the domain/service today, but the API
    only exposes it starting at a later batch; nothing here anticipates
    that endpoint.
    """

    SEED = "seed"
    HIT_REVIEW = "hit_review"


class SampleAcquisitionOutcome(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


_ACQUISITION_HASH_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleAcquisitionAttempt:
    """Append-only, investigation-scoped ledger entry for one VT byte pull.

    A prior `SUCCESS` row for the same `(investigation_id, requested_hash)`
    pair is the canonical replay marker: finding one means no budget is
    consumed and no network call is made.  The DB enforces at most one such
    row per pair (see the 0005 migration); this dataclass only enforces the
    shape of a single row.
    """

    investigation_id: UUID
    requested_hash: str
    hash_family: str
    reason: SampleAcquisitionReason
    outcome: SampleAcquisitionOutcome
    sample_id: UUID | None = None
    error_code: str | None = None
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not _ACQUISITION_HASH_RE.fullmatch(self.requested_hash):
            raise ValueError("requested_hash must be a lowercase MD5, SHA-1, or SHA-256")
        if self.hash_family not in ("md5", "sha1", "sha256"):
            raise ValueError("hash_family must be md5, sha1, or sha256")
        if self.outcome is SampleAcquisitionOutcome.SUCCESS and self.sample_id is None:
            raise ValueError("a successful acquisition must reference a sample")
        if self.outcome is SampleAcquisitionOutcome.ERROR and not self.error_code:
            raise ValueError("a failed acquisition must record an error_code")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")


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

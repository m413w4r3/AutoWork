"""Domain models for subject production workflows."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceCandidate, SourceRole


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


class ProductionBatchPhase(StrEnum):
    INITIAL = "initial"
    RECOVERY = "recovery"
    REVIEW = "review"


class ExtractionProfile(StrEnum):
    """The explicit scope of a source-level Q2 extraction."""

    FULL = "full"
    IOC_RULES = "ioc_rules"


class DetectionRuleType(StrEnum):
    YARA = "yara"
    SIGMA = "sigma"
    SURICATA = "suricata"
    SNORT = "snort"


@dataclass(frozen=True)
class DetectionRule:
    """A published detection rule kept as canonical, inert source data."""

    rule_type: DetectionRuleType
    name: str | None
    body: str
    source_ids: tuple[str, ...]
    context: str
    evidence_quote: str
    supported: bool
    model_run_ids: tuple[str, ...]
    sha256: str


class SourceExtractionStatus(StrEnum):
    """Lifecycle of a source-level extraction checkpoint."""

    RUNNING = "running"
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionInputSource:
    """The source-candidate metadata captured for a production run."""

    batch_id: UUID
    candidate_id: UUID
    source_candidate_id: UUID
    canonical_url: str
    role: SourceRole
    title: str
    publisher: str
    published_at: date | None
    tlp: TLP
    sensitivity: str
    external_llm_allowed: bool

    def __post_init__(self) -> None:
        if not self.canonical_url.strip():
            raise ValueError("A production input source requires a canonical URL")
        if not self.title.strip() or not self.publisher.strip():
            raise ValueError("A production input source requires title and publisher")
        if not self.sensitivity.strip():
            raise ValueError("A production input source requires sensitivity")

    def payload(self) -> dict[str, object]:
        return {
            "batch_id": str(self.batch_id),
            "candidate_id": str(self.candidate_id),
            "source_candidate_id": str(self.source_candidate_id),
            "canonical_url": self.canonical_url,
            "role": self.role.value,
            "title": self.title,
            "publisher": self.publisher,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "tlp": self.tlp.value,
            "sensitivity": self.sensitivity,
            "external_llm_allowed": self.external_llm_allowed,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ProductionInputSource:
        published_at = payload.get("published_at")
        return cls(
            batch_id=UUID(str(payload["batch_id"])),
            candidate_id=UUID(str(payload["candidate_id"])),
            source_candidate_id=UUID(str(payload["source_candidate_id"])),
            canonical_url=str(payload["canonical_url"]),
            role=SourceRole(str(payload["role"])),
            title=str(payload["title"]),
            publisher=str(payload["publisher"]),
            published_at=date.fromisoformat(str(published_at)) if published_at else None,
            tlp=TLP(str(payload["tlp"])),
            sensitivity=str(payload["sensitivity"]),
            external_llm_allowed=bool(payload["external_llm_allowed"]),
        )

    def to_source_candidate(self) -> SourceCandidate:
        """Rehydrate only the fields needed by the collection metadata path."""
        return SourceCandidate(
            id=self.source_candidate_id,
            url=self.canonical_url,
            title=self.title,
            publisher=self.publisher,
            role=self.role,
            published_at=self.published_at,
            tlp=self.tlp,
            sensitivity=self.sensitivity,
            external_llm_allowed=self.external_llm_allowed,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionInputSnapshot:
    """Immutable functional input captured exactly once for a production run."""

    production_run_id: UUID
    subject_id: UUID
    edition_id: UUID
    editorial_group_id: UUID
    editorial_group_version: int
    subject_title: str
    subject_description: str
    actor_or_campaign: str
    period_start: date
    period_end: date
    research_date: date
    core_sources: tuple[ProductionInputSource, ...] = ()
    input_hash: str = ""
    reuse_basis_hash: str = ""
    id: UUID = field(default_factory=uuid4)
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "core_sources",
            tuple(
                sorted(
                    self.core_sources,
                    key=lambda source: (
                        source.canonical_url,
                        str(source.batch_id),
                        str(source.candidate_id),
                        str(source.source_candidate_id),
                    ),
                )
            ),
        )
        if self.editorial_group_version < 1:
            raise ValueError("editorial_group_version must be >= 1")
        if self.period_start > self.period_end:
            raise ValueError("Production input period must be ordered")
        if not self.subject_title.strip():
            raise ValueError("A production input snapshot requires a subject title")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        computed_basis = self.compute_reuse_basis_hash()
        if self.reuse_basis_hash and self.reuse_basis_hash != computed_basis:
            raise ValueError("reuse_basis_hash does not match the functional snapshot payload")
        object.__setattr__(self, "reuse_basis_hash", computed_basis)
        computed = self.compute_input_hash()
        if self.input_hash and self.input_hash != computed:
            raise ValueError("input_hash does not match the functional snapshot payload")
        object.__setattr__(self, "input_hash", computed)

    def reuse_basis_payload(self) -> dict[str, object]:
        return {
            "subject_id": str(self.subject_id),
            "edition_id": str(self.edition_id),
            "editorial_group_id": str(self.editorial_group_id),
            "editorial_group_version": self.editorial_group_version,
            "subject_title": self.subject_title,
            "subject_description": self.subject_description,
            "actor_or_campaign": self.actor_or_campaign,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "core_sources": [source.payload() for source in self.core_sources],
        }

    def functional_payload(self) -> dict[str, object]:
        payload = self.reuse_basis_payload()
        payload["research_date"] = self.research_date.isoformat()
        return payload

    def compute_reuse_basis_hash(self) -> str:
        encoded = json.dumps(
            self.reuse_basis_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def compute_input_hash(self) -> str:
        encoded = json.dumps(
            self.functional_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def production_stages() -> tuple[SubjectProductionStage, ...]:
    """Return the one executable publication pipeline."""
    return (
        SubjectProductionStage.SOURCES,
        SubjectProductionStage.REFERENCES,
        SubjectProductionStage.EXTRACTION,
        SubjectProductionStage.SYNTHESIS,
        SubjectProductionStage.ASSEMBLY,
    )


def next_stage(stage: SubjectProductionStage) -> SubjectProductionStage | None:
    """Return the successor in the unified pipeline."""
    if not isinstance(stage, SubjectProductionStage):
        raise TypeError("next_stage requires a SubjectProductionStage")
    stages = production_stages()
    try:
        index = stages.index(stage)
    except ValueError as exc:
        raise ValueError(f"Stage {stage.value} is not valid for the publication pipeline") from exc
    return stages[index + 1] if index + 1 < len(stages) else None


class ProductionArtifactStage(StrEnum):
    REFERENCES = "references"
    EXTRACTION = "extraction"
    SYNTHESIS = "synthesis"
    PUBLICATION = "publication"


class ProductionArtifactStatus(StrEnum):
    VERIFIED = "verified"
    STALE = "stale"
    NEEDS_REVIEW = "needs_review"


@dataclass(slots=True, kw_only=True)
class SourceExtraction:
    """Subject-independent, content-addressed Q2 extraction checkpoint."""

    canonical_url: str
    source_content_sha256: str
    profile: ExtractionProfile
    contract_version: str
    prompt_version: str
    parser_version: str
    verifier_version: str
    status: SourceExtractionStatus = SourceExtractionStatus.RUNNING
    canonical_blob_id: UUID | None = None
    raw_blob_id: UUID | None = None
    model_run_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.canonical_url.strip():
            raise ValueError("A source extraction requires a canonical URL")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_content_sha256):
            raise ValueError("source_content_sha256 must be a lowercase SHA-256")
        for name, value in (
            ("contract_version", self.contract_version),
            ("prompt_version", self.prompt_version),
            ("parser_version", self.parser_version),
            ("verifier_version", self.verifier_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")


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
    # A deliberate user retry bypasses cross-run reuse for this stage and all
    # downstream costly stages.  Technical retries of the same job keep using
    # the persisted artifact of the same run.
    force_recompute_from_stage: SubjectProductionStage | None = None
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
        if self.research_date is None:
            # The boundary is frozen at run creation.  In particular, a queued
            # run must not choose a different date when a worker starts it.
            self.research_date = self.created_at.date()

    def start_running(self, *, now: datetime | None = None) -> None:
        if self.status is not SubjectProductionStatus.QUEUED:
            raise ValueError("Can only start from QUEUED status")
        self.status = SubjectProductionStatus.RUNNING
        self.started_at = now or datetime.now(UTC)
        if self.research_date is None:
            raise ValueError("research_date must be frozen before a run starts")
        self.updated_at = self.started_at
        self.version += 1

    def advance_stage(self, *, now: datetime | None = None) -> None:
        if self.status is SubjectProductionStatus.CANCELLED:
            raise ValueError("production_run_cancelled")
        successor = next_stage(self.current_stage)
        if successor is not None:
            self.current_stage = successor
        self.updated_at = now or datetime.now(UTC)
        self.version += 1

    def mark_ready(self, *, now: datetime | None = None) -> None:
        """READY implies assembly complete and QA passed."""
        if self.status is SubjectProductionStatus.READY:
            return
        if self.status is SubjectProductionStatus.CANCELLED:
            raise ValueError("production_run_cancelled")
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
        if self.status is SubjectProductionStatus.CANCELLED:
            raise ValueError("production_run_cancelled")
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
        if self.status is SubjectProductionStatus.CANCELLED:
            raise ValueError("production_run_cancelled")
        self.status = SubjectProductionStatus.FAILED
        self.error_code = code[:64]
        self.error_message = " ".join(message.replace("\x00", "").split())[:500]
        self.error_details = details
        self.finished_at = now or datetime.now(UTC)
        self.updated_at = self.finished_at
        self.version += 1

    def mark_cancelled(self, *, now: datetime | None = None) -> None:
        if self.status is SubjectProductionStatus.CANCELLED:
            return
        if self.status not in {
            SubjectProductionStatus.QUEUED,
            SubjectProductionStatus.RUNNING,
        }:
            raise ValueError("production_run_not_cancellable")
        self.status = SubjectProductionStatus.CANCELLED
        self.finished_at = now or datetime.now(UTC)
        self.updated_at = self.finished_at
        self.version += 1

    def retry_from_stage(
        self,
        stage: SubjectProductionStage,
        *,
        now: datetime | None = None,
        force_recompute: bool = True,
    ) -> None:
        """Start a deliberate new pipeline generation at ``stage``.

        Unlike a worker retry, this invalidates the selected stage and its
        downstream outputs.  Callers must validate prerequisites first.
        """
        if self.status is SubjectProductionStatus.CANCELLED:
            raise ValueError("production_run_cancelled")
        if self.status in (SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING):
            raise ValueError("Cannot retry a queued or running production")
        self.status = SubjectProductionStatus.RUNNING
        self.current_stage = stage
        self.pipeline_generation += 1
        if force_recompute:
            self.force_recompute_from_stage = {
                SubjectProductionStage.SOURCES: SubjectProductionStage.REFERENCES,
                SubjectProductionStage.REFERENCES: SubjectProductionStage.REFERENCES,
                SubjectProductionStage.EXTRACTION: SubjectProductionStage.EXTRACTION,
                SubjectProductionStage.SYNTHESIS: SubjectProductionStage.SYNTHESIS,
                SubjectProductionStage.ASSEMBLY: None,
            }[stage]
        else:
            self.force_recompute_from_stage = None
        if stage is SubjectProductionStage.SOURCES:
            self.references_conversation_id = None
            self.synthesis_conversation_id = None
        elif stage is SubjectProductionStage.REFERENCES:
            self.references_conversation_id = None
            self.synthesis_conversation_id = None
        elif stage is SubjectProductionStage.EXTRACTION:
            self.synthesis_conversation_id = None
        elif stage is SubjectProductionStage.SYNTHESIS:
            self.synthesis_conversation_id = None
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
    reused_from_artifact_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if len(self.input_hash) != 64 or any(c not in "0123456789abcdef" for c in self.input_hash):
            raise ValueError("input_hash must be lowercase SHA-256")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductionReuseInvalidation:
    """Append-only operator request that limits future cross-run reuse."""

    edition_id: UUID
    subject_id: UUID
    from_stage: SubjectProductionStage
    actor_id: str
    correlation_id: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.from_stage not in {
            SubjectProductionStage.REFERENCES,
            SubjectProductionStage.EXTRACTION,
            SubjectProductionStage.SYNTHESIS,
        }:
            raise ValueError("Production reuse invalidation must start at a costly stage")
        if not self.actor_id.strip():
            raise ValueError("Production reuse invalidation requires an actor")
        if not self.correlation_id.strip():
            raise ValueError("Production reuse invalidation requires a correlation id")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")


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


class ProductionBatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    CANCELLED = "cancelled"


@dataclass(slots=True, kw_only=True)
class EditionProductionBatch:
    edition_id: UUID
    status: ProductionBatchStatus
    phase: ProductionBatchPhase = ProductionBatchPhase.INITIAL
    next_dispatch_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProductionBatchStatus):
            self.status = ProductionBatchStatus(str(self.status))
        if not isinstance(self.phase, ProductionBatchPhase):
            self.phase = ProductionBatchPhase(str(self.phase))

    def start(self, *, now: datetime | None = None) -> None:
        if self.status is not ProductionBatchStatus.QUEUED:
            raise ValueError("Can only start from queued status")
        self.status = ProductionBatchStatus.RUNNING
        self.started_at = now or datetime.now(UTC)
        self.version += 1

    def finish(self, *, completed_with_issues: bool = False, now: datetime | None = None) -> None:
        if self.status is not ProductionBatchStatus.RUNNING:
            raise ValueError("Can only finish from running status")
        self.status = (
            ProductionBatchStatus.COMPLETED_WITH_ISSUES
            if completed_with_issues
            else ProductionBatchStatus.COMPLETED
        )
        self.finished_at = now or datetime.now(UTC)
        self.version += 1

    def cancel(self, *, now: datetime | None = None) -> None:
        if self.status is ProductionBatchStatus.CANCELLED:
            return
        self.status = ProductionBatchStatus.CANCELLED
        self.next_dispatch_at = None
        self.finished_at = now or datetime.now(UTC)
        self.version += 1

    def schedule_next_dispatch(self, dispatch_at: datetime) -> None:
        if dispatch_at.tzinfo is None or dispatch_at.utcoffset() is None:
            raise ValueError("dispatch_at must be timezone-aware")
        self.next_dispatch_at = dispatch_at
        self.version += 1

    def clear_next_dispatch(self) -> None:
        if self.next_dispatch_at is not None:
            self.next_dispatch_at = None
            self.version += 1

    def enter_recovery(self) -> None:
        if self.status is not ProductionBatchStatus.RUNNING:
            raise ValueError("Can only enter recovery from a running batch")
        self.phase = ProductionBatchPhase.RECOVERY
        self.version += 1

    def enter_review(self) -> None:
        self.phase = ProductionBatchPhase.REVIEW
        self.next_dispatch_at = None
        self.version += 1


@dataclass(slots=True, kw_only=True)
class EditionProductionBatchItem:
    batch_id: UUID
    subject_id: UUID
    production_run_id: UUID
    position: int
    auto_recovery_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.position < 1:
            raise ValueError("position must be >= 1")
        if self.auto_recovery_count not in (0, 1):
            raise ValueError("auto_recovery_count must be between 0 and 1")

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from cti_app.domain.discovery import CandidateTopic, DiscoverySourceMode


class DiscoveryInputMode(StrEnum):
    BRIDGE_RESEARCH = "bridge_research"
    MANUAL_IMPORT = "manual_import"
    RECOVERY = "recovery"


class DiscoveryIdentityStatus(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"


class DiscoveryPlannerKind(StrEnum):
    DETERMINISTIC_BOOTSTRAP = "deterministic_bootstrap"
    HEURISTIC = "heuristic"
    CHATGPT = "chatgpt"
    HUMAN = "human"


class DiscoverySnapshotLineage(StrEnum):
    OPERATIONAL = "operational"
    REPLAY = "replay"


class MergeValidationStatus(StrEnum):
    VALID = "valid"
    REPAIRED = "repaired"
    INVALID = "invalid"
    NEEDS_REVIEW = "needs_review"
    # A run that was awaiting review and has since been settled by a human. It
    # keeps its plan for lineage, but it is no longer actionable: leaving it on
    # NEEDS_REVIEW is what made the review panel outlive the decision it applied.
    RESOLVED = "resolved"


class MergeConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MergeDisposition(StrEnum):
    APPLY = "apply"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class DiscoveryIntake:
    edition_id: UUID
    sequence: int
    input_mode: DiscoveryInputMode
    raw_report_hash: str
    parsed_report_hash: str
    intake_hash: str
    research_model_run_id: UUID
    source_mode: DiscoverySourceMode
    complementary_axis: str
    batch_id: UUID
    created_by: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("Discovery intake sequence must be positive")
        for value in (self.raw_report_hash, self.parsed_report_hash, self.intake_hash):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError("Discovery intake hashes must be lowercase SHA-256 values")
        if not self.created_by.strip():
            raise ValueError("Discovery intake creator is required")


@dataclass(frozen=True, slots=True)
class DiscoverySubjectIdentity:
    edition_id: UUID
    origin_key: str
    created_by_merge_run_id: UUID
    id: UUID
    cross_edition_lineage_id: UUID | None = None
    status: DiscoveryIdentityStatus = DiscoveryIdentityStatus.ACTIVE
    merged_into_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.origin_key:
            raise ValueError("Discovery subject origin key is required")
        if self.status is DiscoveryIdentityStatus.ACTIVE and self.merged_into_id is not None:
            raise ValueError("An active discovery identity cannot have a merge target")
        if self.status is DiscoveryIdentityStatus.MERGED and self.merged_into_id is None:
            raise ValueError("A merged discovery identity requires a merge target")


@dataclass(frozen=True, slots=True)
class DiscoveryMemberReference:
    batch_id: UUID
    candidate_id: UUID


@dataclass(frozen=True, slots=True)
class DiscoverySubject:
    subject_id: UUID
    candidate: CandidateTopic
    member_references: tuple[DiscoveryMemberReference, ...]
    created_at: datetime

    @property
    def canonical_title(self) -> str:
        return self.candidate.title

    @property
    def canonical_summary(self) -> str:
        return self.candidate.summary


@dataclass(frozen=True, slots=True)
class SubjectContribution:
    subject_id: UUID
    intake_id: UUID
    candidate_key: UUID
    candidate_id: UUID
    first_seen_snapshot_id: UUID
    first_seen_version: int
    contributed_title: str
    contributed_summary: str
    contributed_source_ids: tuple[UUID, ...]
    contributed_provisional_ioc_ids: tuple[UUID, ...]
    merge_run_id: UUID
    merge_group_index: int
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.first_seen_version < 1 or self.merge_group_index < 0:
            raise ValueError("Invalid contribution snapshot version or merge group")


@dataclass(frozen=True, slots=True)
class SubjectMergeEvent:
    edition_id: UUID
    from_subject_id: UUID
    into_subject_id: UUID
    merge_run_id: UUID
    actor_id: str
    reason: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.from_subject_id == self.into_subject_id:
            raise ValueError("A subject cannot be merged into itself")
        if not self.actor_id.strip() or not self.reason.strip():
            raise ValueError("Subject merge actor and reason are required")


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    edition_id: UUID
    version: int
    parent_snapshot_id: UUID | None
    intake_id: UUID
    merge_run_id: UUID
    planner_kind: DiscoveryPlannerKind
    subjects: tuple[DiscoverySubject, ...]
    snapshot_hash: str
    lineage: DiscoverySnapshotLineage = DiscoverySnapshotLineage.OPERATIONAL
    replay_run_id: UUID | None = None
    is_active: bool = False
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Discovery snapshot version must be positive")
        if len(self.snapshot_hash) != 64:
            raise ValueError("Discovery snapshot hash must be a SHA-256")
        ids = [subject.subject_id for subject in self.subjects]
        if len(ids) != len(set(ids)):
            raise ValueError("A snapshot cannot contain the same subject twice")


@dataclass(frozen=True, slots=True)
class DiscoveryMergeRun:
    edition_id: UUID
    parent_snapshot_id: UUID | None
    intake_id: UUID
    planner_kind: DiscoveryPlannerKind
    prompt_version: str
    policy_version: str
    blocking_version: str
    merge_input_hash: str
    handle_map: dict[str, str]
    included_subject_ids: tuple[UUID, ...]
    excluded_subject_count: int
    validation_status: MergeValidationStatus
    warnings: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    plan_payload: dict[str, object] | None = None
    supersedes_merge_run_id: UUID | None = None
    merge_model_run_id: UUID | None = None
    raw_output_reference: str | None = None
    normalized_output_reference: str | None = None
    rebase_count: int = 0
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if len(self.merge_input_hash) != 64:
            raise ValueError("Merge input hash must be a SHA-256")
        if self.excluded_subject_count < 0 or not 0 <= self.rebase_count <= 2:
            raise ValueError("Invalid merge run counters")


class MergeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shared_publication_urls: list[str] = Field(default_factory=list)
    shared_campaigns: list[str] = Field(default_factory=list)
    shared_malware: list[str] = Field(default_factory=list)
    shared_explicit_identifiers: list[str] = Field(default_factory=list)
    semantic_basis: list[str] = Field(default_factory=list)
    conflict_signals: list[str] = Field(default_factory=list)


class DiscoveryMergeGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    existing_subject_handles: list[str]
    incoming_candidate_handles: list[str] = Field(min_length=1)
    confidence: MergeConfidence
    disposition: MergeDisposition
    rationale: str
    evidence: MergeEvidence = Field(default_factory=MergeEvidence)
    flags: list[str] = Field(default_factory=list)


class DiscoveryMergePlanV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    groups: list[DiscoveryMergeGroup]
    warnings: list[str] = Field(default_factory=list)


def discovery_candidate_key(intake_id: UUID, local_ref: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"discovery-candidate:{intake_id}:{local_ref}")


def discovery_origin_key(candidate_keys: list[UUID] | tuple[UUID, ...]) -> str:
    if not candidate_keys:
        raise ValueError("A subject origin requires at least one candidate")
    return ":".join(sorted(str(key) for key in candidate_keys))


def discovery_subject_id(edition_id: UUID, origin_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"discovery-subject:{edition_id}:{origin_key}")


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


# Incrément 4: Replay and identity mapping
class ReplayIdentityResolution(StrEnum):
    """How a replay identity relates to the operational one."""
    SAME = "same"  # Same origin_key, same subject_id
    SPLIT_OF = "split_of"  # Replay split what was merged in operational
    MERGE_OF = "merge_of"  # Replay merged what was separate in operational
    NEW = "new"  # New subject in replay, no operational equivalent


@dataclass(frozen=True, slots=True)
class ReplayIdentityMapping:
    """
    Maps a subject identity from replay run to operational identity.

    Part of replay activation workflow (D14):
    - Replay produces different snapshots due to different merge logic
    - Mapping bridges replay identities to operational references
    - Activation precondition: published artifacts must have mapping
    """
    replay_run_id: UUID
    replay_subject_id: UUID
    operational_subject_id: UUID | None  # Nullable if new in replay
    resolution: ReplayIdentityResolution
    actor_id: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.resolution == ReplayIdentityResolution.SAME and self.operational_subject_id is None:
            raise ValueError("SAME resolution requires an operational subject")
        if self.resolution == ReplayIdentityResolution.NEW and self.operational_subject_id is not None:
            raise ValueError("NEW resolution must not have an operational subject")
        if not self.actor_id.strip():
            raise ValueError("Replay identity mapping requires an actor")


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    """
    Summary of differences between a replay and operational timeline.

    Incrément 4: For display to operators and decision-making.
    """
    replay_run_id: UUID
    edition_id: UUID
    subjects_same_count: int
    subjects_split_count: int
    subjects_merged_count: int
    subjects_created_count: int
    subjects_impacting_editorial: int  # Subjects with published artifacts affected
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if any(v < 0 for v in [
            self.subjects_same_count,
            self.subjects_split_count,
            self.subjects_merged_count,
            self.subjects_created_count,
            self.subjects_impacting_editorial,
        ]):
            raise ValueError("Replay comparison counts must be non-negative")

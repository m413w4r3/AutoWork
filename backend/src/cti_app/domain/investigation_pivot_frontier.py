"""Versioned, reconstructible pivot-frontier graph contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PIVOT_FRONTIER_SCHEMA_VERSION = "investigation-pivot-frontier.v1"


class PivotQualificationLevel(StrEnum):
    DOCUMENTED = "documented"
    DISCOVERED = "discovered"
    CANDIDATE = "candidate"
    ASSOCIATED = "associated"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"


class PivotExecutionState(StrEnum):
    QUEUED = "queued"
    EXECUTED = "executed"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"


class PivotG2State(StrEnum):
    REQUIRED = "required"
    APPROVED = "approved"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class PivotDisclosureState(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class PivotPriorityFactors:
    novelty_vs_article: int
    novelty_vs_verified: int
    depth: int
    cost: int
    formula: str = "clamp(50*article_novelty + 30*verified_novelty + 5/depth - 5*cost, 0, 100)"


@dataclass(frozen=True, slots=True)
class PivotFrontierRecord:
    """One idempotent qualification of a canonical indicator from one reason/source."""

    id: str
    canonical_id: str
    indicator_type: str
    value: str
    parent_id: str | None
    path: tuple[str, ...]
    source_observation_id: str | None
    source: str
    source_version: str
    source_sha256: str
    level: PivotQualificationLevel
    reason: str
    confidence: float
    limitations: tuple[str, ...]
    novelty_vs_article: bool
    novelty_vs_verified: bool
    estimated_cost: int
    g2_state: PivotG2State
    disclosure_state: PivotDisclosureState
    priority_score: int
    priority_factors: PivotPriorityFactors
    state: PivotExecutionState
    state_reason: str
    cursor: str | None
    pagination_complete: bool | None
    children_observed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PivotFrontierEdge:
    id: str
    parent_id: str
    child_id: str
    relation: str
    source_observation_id: str
    source_version: str
    source_sha256: str
    reason: str
    confidence: float
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PivotFrontierPath:
    id: str
    root_id: str
    target_id: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PivotFrontierV1:
    schema_version: str
    investigation_id: str
    source_sha256: str
    frontier_sha256: str
    records: tuple[PivotFrontierRecord, ...]
    edges: tuple[PivotFrontierEdge, ...]
    paths: tuple[PivotFrontierPath, ...]
    truncated: bool = False
    next_cursor: str | None = None

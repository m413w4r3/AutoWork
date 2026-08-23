from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cti_app.domain.discovery import CandidateTopic
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergePlanV1,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
    SubjectContribution,
    SubjectMergeEvent,
)


@dataclass(frozen=True, slots=True)
class IncomingDiscoveryCandidate:
    handle: str
    candidate_key: UUID
    candidate: CandidateTopic
    batch_id: UUID


@dataclass(frozen=True, slots=True)
class DiscoveryDelta:
    intake_id: UUID
    candidates: tuple[IncomingDiscoveryCandidate, ...]
    delta_hash: str


@dataclass(frozen=True, slots=True)
class ResolvedMergeHandles:
    existing: dict[str, UUID]
    incoming: dict[str, IncomingDiscoveryCandidate]


@dataclass(frozen=True, slots=True)
class AppliedDiscoveryMerge:
    snapshot: DiscoverySnapshot
    identities: tuple[DiscoverySubjectIdentity, ...]
    contributions: tuple[SubjectContribution, ...]
    merge_events: tuple[SubjectMergeEvent, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlannedDiscoveryMerge:
    plan: DiscoveryMergePlanV1
    merge_model_run_id: UUID | None = None
    raw_output_reference: str | None = None
    normalized_output_reference: str | None = None
    validation_status: MergeValidationStatus = MergeValidationStatus.VALID
    warnings: tuple[str, ...] = ()


class DiscoveryMergePlanner(Protocol):
    kind: DiscoveryPlannerKind
    policy_version: str

    async def plan(
        self,
        parent_snapshot: DiscoverySnapshot | None,
        delta: DiscoveryDelta,
        handles: ResolvedMergeHandles,
        *,
        edition_id: UUID,
        external_llm_allowed: bool,
        sensitivity: str,
    ) -> PlannedDiscoveryMerge: ...


@dataclass(frozen=True, slots=True)
class MergeHandleLabel:
    """What a merge handle stands for, in reviewer-readable terms."""

    handle: str
    title: str
    summary: str
    source_urls: tuple[str, ...]

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.discovery import SourceRelationshipStatus


class GroupingOutcome(StrEnum):
    NEW_SUBJECT = "new_subject"
    DUPLICATE_PUBLICATION = "duplicate_same_publication"
    UPDATE_PREVIOUS = "update_previous_subject"
    NON_INDEPENDENT_REPRINT = "non_independent_reprint"
    AMBIGUOUS_REVIEW = "ambiguous_review"


class EditorialType(StrEnum):
    BRIEF = "brief"
    MAJOR = "major"


class EditorialGroupStatus(StrEnum):
    PROPOSED = "proposed"
    REJECTED = "rejected"
    SELECTED = "selected"
    SUPERSEDED = "superseded"


class GroupingConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HumanDecisionType(StrEnum):
    MERGE = "merge"
    SPLIT = "split"
    REJECT = "reject"
    SELECT = "select"
    CLAIM_VALIDATE = "claim_validate"
    CLAIM_CORRECT = "claim_correct"
    CLAIM_REJECT = "claim_reject"
    INDICATOR_VALIDATE = "indicator_validate"
    INDICATOR_CORRECT = "indicator_correct"
    INDICATOR_REJECT = "indicator_reject"
    SOURCE_RELATIONSHIP_VALIDATE = "source_relationship_validate"
    SOURCE_RELATIONSHIP_CORRECT = "source_relationship_correct"
    BRIEF_CHANGES_REQUESTED = "brief_changes_requested"
    BRIEF_APPROVE = "brief_approve"
    BRIEF_PROMOTE = "brief_promote"


# Incrément 3: Préservation éditoriale
class EditorialUpdateDecisionAction(StrEnum):
    """Action on an editorial artifact regarding new contributions."""
    DISMISS = "dismiss"
    RESTORE = "restore"


@dataclass(frozen=True, slots=True)
class CandidateReference:
    batch_id: UUID
    candidate_id: UUID


@dataclass(frozen=True, slots=True)
class EditorialScore:
    impact: int
    novelty: int
    technical_depth: int
    hunting_potential: int
    actionability: int
    source_quality: int
    justifications: dict[str, str]

    def __post_init__(self) -> None:
        values = (
            self.impact,
            self.novelty,
            self.technical_depth,
            self.hunting_potential,
            self.actionability,
            self.source_quality,
        )
        if any(value < 0 or value > 4 for value in values):
            raise ValueError("Editorial score dimensions must be between 0 and 4")

    @property
    def total(self) -> int:
        return sum(
            (
                self.impact,
                self.novelty,
                self.technical_depth,
                self.hunting_potential,
                self.actionability,
                self.source_quality,
            )
        )


@dataclass(slots=True)
class EditorialGroup:
    edition_id: UUID
    title: str
    candidate_references: tuple[CandidateReference, ...]
    outcome: GroupingOutcome
    score: EditorialScore
    source_relationship_status: SourceRelationshipStatus
    needs_source_verification: bool
    needs_source_expansion: bool
    grouping_confidence: GroupingConfidence
    grouping_justification: str
    id: UUID = field(default_factory=uuid4)
    potential_historical_group_id: UUID | None = None
    status: EditorialGroupStatus = EditorialGroupStatus.PROPOSED
    editorial_type: EditorialType | None = None
    subject_id: UUID | None = None
    discovery_subject_id: UUID | None = None
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        self.grouping_justification = self.grouping_justification.strip()
        self.candidate_references = tuple(dict.fromkeys(self.candidate_references))
        if not self.title or not self.candidate_references or not self.grouping_justification:
            raise ValueError("Editorial group title, candidates and justification are required")
        if self.source_relationship_status is SourceRelationshipStatus.PROVISIONAL:
            self.needs_source_verification = True

    def add_candidates(self, references: tuple[CandidateReference, ...]) -> None:
        # Allow enrichment of both PROPOSED and SELECTED groups (P2: enrichment of selected groups)
        if self.status not in (EditorialGroupStatus.PROPOSED, EditorialGroupStatus.SELECTED):
            raise ValueError("Only proposed or selected groups can be enriched")
        self.candidate_references = tuple(dict.fromkeys((*self.candidate_references, *references)))
        self._bump()

    def remove_candidates(self, references: set[CandidateReference]) -> None:
        if self.status is not EditorialGroupStatus.PROPOSED:
            raise ValueError("Only proposed groups can be split")
        remaining = tuple(item for item in self.candidate_references if item not in references)
        if not remaining:
            raise ValueError("A split cannot empty its source group")
        self.candidate_references = remaining
        self._bump()

    def replace_candidate_references(
        self, replacements: dict[CandidateReference, CandidateReference]
    ) -> None:
        updated = tuple(replacements.get(item, item) for item in self.candidate_references)
        updated = tuple(dict.fromkeys(updated))
        if updated != self.candidate_references:
            self.candidate_references = updated
            self._bump()

    def supersede(self) -> None:
        if self.status is not EditorialGroupStatus.PROPOSED:
            raise ValueError("Only proposed groups can be merged")
        self.status = EditorialGroupStatus.SUPERSEDED
        self._bump()

    def reject(self) -> None:
        if self.status is not EditorialGroupStatus.PROPOSED:
            raise ValueError("Only proposed groups can be rejected")
        self.status = EditorialGroupStatus.REJECTED
        self._bump()

    def select(self, editorial_type: EditorialType, subject_id: UUID) -> None:
        if self.status is not EditorialGroupStatus.PROPOSED:
            raise ValueError("Only proposed groups can be selected")
        self.status = EditorialGroupStatus.SELECTED
        self.editorial_type = editorial_type
        self.subject_id = subject_id
        self._bump()

    def promote_to_major(self) -> None:
        if self.status is not EditorialGroupStatus.SELECTED:
            raise ValueError("Only a selected group can be promoted")
        if self.editorial_type is not EditorialType.BRIEF:
            raise ValueError("Only a brief can be promoted to a major article")
        self.editorial_type = EditorialType.MAJOR
        self._bump()

    def _bump(self) -> None:
        self.version += 1
        self.updated_at = datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class HumanDecision:
    edition_id: UUID
    decision_type: HumanDecisionType
    group_ids: tuple[UUID, ...]
    actor_id: str
    correlation_id: str
    payload: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.group_ids or not self.actor_id.strip() or not self.correlation_id.strip():
            raise ValueError("Human decision requires groups, actor and correlation")


@dataclass(frozen=True, slots=True)
class EditorialUpdateDecision:
    """Log entry for dismissing or restoring UPDATE_AVAILABLE signal (Incrément 3).

    Append-only log: never modified or deleted. State is computed by folding the log
    by (artifact_id, contribution_id), last decision wins.
    """
    edition_id: UUID
    artifact_id: UUID
    action: EditorialUpdateDecisionAction
    contribution_ids: tuple[UUID, ...]
    actor_id: str
    reason: str
    supersedes_decision_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.contribution_ids or not self.actor_id.strip() or not self.reason.strip():
            raise ValueError("Update decision requires contributions, actor and reason")

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class HumanDecisionType(StrEnum):
    CLAIM_VALIDATE = "claim_validate"
    CLAIM_CORRECT = "claim_correct"
    CLAIM_REJECT = "claim_reject"
    INDICATOR_VALIDATE = "indicator_validate"
    INDICATOR_CORRECT = "indicator_correct"
    INDICATOR_REJECT = "indicator_reject"
    SOURCE_RELATIONSHIP_VALIDATE = "source_relationship_validate"
    SOURCE_RELATIONSHIP_CORRECT = "source_relationship_correct"


class AnalystDecisionType(StrEnum):
    MEMBER_VALIDATE = "member_validate"
    MEMBER_REJECT = "member_reject"
    FEATURE_VALIDATE = "feature_validate"
    FEATURE_REJECT = "feature_reject"
    PIVOT_APPROVE = "pivot_approve"
    PIVOT_REJECT = "pivot_reject"
    NOTE_APPROVE = "note_approve"
    NOTE_CHANGES_REQUESTED = "note_changes_requested"


class AnalystDecisionTargetType(StrEnum):
    MEMBER = "member"
    FEATURE = "feature"
    TOOL = "tool"
    INVARIANT = "invariant"
    PIVOT = "pivot"
    CORPUS = "corpus"
    DETECTION = "detection"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class HumanDecision:
    edition_id: UUID
    decision_type: HumanDecisionType
    subject_ids: tuple[UUID, ...]
    actor_id: str
    correlation_id: str
    payload: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.subject_ids or not self.actor_id.strip() or not self.correlation_id.strip():
            raise ValueError("Human decision requires subjects, actor and correlation")


@dataclass(frozen=True, slots=True)
class AnalystDecision:
    investigation_id: UUID
    decision_type: AnalystDecisionType
    target_type: AnalystDecisionTargetType
    target_id: UUID
    actor_id: str
    reason: str
    correlation_id: str
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or not self.reason.strip() or not self.correlation_id.strip():
            raise ValueError("Analyst decision requires actor, reason and correlation")

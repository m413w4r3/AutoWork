"""Small JSON-canonical state for the agentic investigation loop."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from cti_app.domain.investigation import InvestigationAgenda, canonical_json_bytes


class DecisionValidationCode(StrEnum):
    UNKNOWN_HYPOTHESIS_REFERENCE = "unknown_hypothesis_reference"
    FINDING_LINKS_MULTIPLE_HYPOTHESES = "finding_links_multiple_hypotheses"
    DUPLICATE_FINDING_IDENTITY_CONFLICTING_LINKS = "duplicate_finding_identity_conflicting_links"


class HypothesisStatus(StrEnum):
    OPEN = "open"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    ABANDONED = "abandoned"


class StopReason(StrEnum):
    AGENT_FINISHED = "agent_finished"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_NEW_INFORMATION = "no_new_information"
    USER_INTERRUPT = "user_interrupt"
    MODEL_UNAVAILABLE = "model_unavailable"
    PERMISSION_REQUIRED = "permission_required"
    SUBMISSION_NEEDS_REVIEW = "submission_needs_review"


@dataclass(frozen=True, slots=True)
class Hypothesis:
    id: str
    statement: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    evidence_refs: tuple[UUID, ...] = ()
    finding_refs: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.statement.strip():
            raise ValueError("Hypothesis identity and statement are required")
        if not isinstance(self.status, HypothesisStatus):
            raise TypeError("Hypothesis status must use HypothesisStatus")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "finding_refs", tuple(self.finding_refs))

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "status": self.status.value,
            "evidence_refs": [str(ref) for ref in self.evidence_refs],
            "finding_refs": [str(ref) for ref in self.finding_refs],
        }


@dataclass(frozen=True, slots=True)
class InvestigationWorkingState:
    SCHEMA: ClassVar[str] = "investigation-state.v1"

    investigation_id: UUID
    agenda: InvestigationAgenda
    version: int = 1
    hypotheses: tuple[Hypothesis, ...] = ()
    turn_index: int = 0
    last_context_sha256: str | None = None
    last_decision_sha256: str | None = None
    pending_context_snapshot_id: UUID | None = None
    pending_context_sha256: str | None = None
    stop_reason: StopReason | None = None
    no_progress_streak: int = 0
    deduplicated_findings: int = 0
    last_seen_evidence_ids: tuple[UUID, ...] = ()
    last_seen_finding_ids: tuple[UUID, ...] = ()
    recent_actions: tuple[str, ...] = ()
    base_publication_artifact_id: UUID | None = None
    base_publication_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            self.version < 1
            or self.turn_index < 0
            or self.no_progress_streak < 0
            or self.deduplicated_findings < 0
        ):
            raise ValueError("Investigation state counters are invalid")
        if self.stop_reason is not None and not isinstance(self.stop_reason, StopReason):
            raise TypeError("stop_reason must use StopReason")
        if (self.base_publication_artifact_id is None) != (self.base_publication_sha256 is None):
            raise ValueError("Base publication identity and SHA-256 must be supplied together")
        if (self.pending_context_snapshot_id is None) != (self.pending_context_sha256 is None):
            raise ValueError(
                "Pending context snapshot identity and SHA-256 must be supplied together"
            )
        for name in (
            "last_context_sha256",
            "last_decision_sha256",
            "pending_context_sha256",
            "base_publication_sha256",
        ):
            value = getattr(self, name)
            if value is not None and (
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if any(not isinstance(item, Hypothesis) for item in self.hypotheses):
            raise TypeError("hypotheses must contain Hypothesis records")
        object.__setattr__(self, "hypotheses", tuple(self.hypotheses))
        object.__setattr__(self, "last_seen_evidence_ids", tuple(self.last_seen_evidence_ids))
        object.__setattr__(self, "last_seen_finding_ids", tuple(self.last_seen_finding_ids))
        object.__setattr__(self, "recent_actions", tuple(self.recent_actions[-32:]))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "investigation_id": str(self.investigation_id),
            "version": self.version,
            "agenda": [
                {"question": item.question, "potential": item.potential, "rank": item.rank}
                for item in self.agenda.items
            ],
            "hypotheses": [hypothesis.to_payload() for hypothesis in self.hypotheses],
            "turn_index": self.turn_index,
            "last_context_sha256": self.last_context_sha256,
            "last_decision_sha256": self.last_decision_sha256,
            "pending_context_snapshot_id": (
                str(self.pending_context_snapshot_id) if self.pending_context_snapshot_id else None
            ),
            "pending_context_sha256": self.pending_context_sha256,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "no_progress_streak": self.no_progress_streak,
            "deduplicated_findings": self.deduplicated_findings,
            "last_seen_evidence_ids": [str(item) for item in self.last_seen_evidence_ids],
            "last_seen_finding_ids": [str(item) for item in self.last_seen_finding_ids],
            "recent_actions": list(self.recent_actions),
            "base_publication_artifact_id": (
                str(self.base_publication_artifact_id)
                if self.base_publication_artifact_id
                else None
            ),
            "base_publication_sha256": self.base_publication_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_payload())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def with_turn(
        self,
        *,
        context_sha256: str,
        decision_sha256: str,
        progress: bool,
        evidence_ids: tuple[UUID, ...],
        finding_ids: tuple[UUID, ...],
        action_summaries: tuple[str, ...],
        hypotheses: tuple[Hypothesis, ...] | None = None,
    ) -> InvestigationWorkingState:
        return replace(
            self,
            version=self.version + 1,
            hypotheses=self.hypotheses if hypotheses is None else hypotheses,
            turn_index=self.turn_index + 1,
            last_context_sha256=context_sha256,
            last_decision_sha256=decision_sha256,
            pending_context_snapshot_id=None,
            pending_context_sha256=None,
            no_progress_streak=0 if progress else self.no_progress_streak + 1,
            last_seen_evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            last_seen_finding_ids=tuple(dict.fromkeys(finding_ids)),
            recent_actions=(*self.recent_actions, *action_summaries)[-32:],
            stop_reason=None,
        )

    def with_pending_context(
        self, snapshot_id: UUID, context_sha256: str
    ) -> InvestigationWorkingState:
        if (
            self.pending_context_snapshot_id == snapshot_id
            and self.pending_context_sha256 == context_sha256
        ):
            return self
        if not isinstance(snapshot_id, UUID):
            raise TypeError("Pending context snapshot id must be a UUID")
        return replace(
            self,
            version=self.version + 1,
            pending_context_snapshot_id=snapshot_id,
            pending_context_sha256=context_sha256,
        )

    def with_bridge_validation_failure(self, detail: str) -> InvestigationWorkingState:
        summary = detail.strip()[:512]
        if not summary:
            summary = "Bridge returned output that failed decision schema validation."
        return replace(
            self,
            version=self.version + 1,
            pending_context_snapshot_id=None,
            pending_context_sha256=None,
            recent_actions=(
                *self.recent_actions,
                f"bridge_invalid_output:{summary}",
            )[-32:],
        )

    def with_context_limit_failure(
        self, *, actual_bytes: int, max_bytes: int
    ) -> InvestigationWorkingState:
        return replace(
            self,
            version=self.version + 1,
            recent_actions=(
                *self.recent_actions,
                f"investigation_context_too_large:{actual_bytes}>{max_bytes}",
            )[-32:],
        )

    def conclude(self, reason: StopReason) -> InvestigationWorkingState:
        if not isinstance(reason, StopReason):
            raise TypeError("reason must use StopReason")
        # A durable stop decision wins races with an in-flight worker. The
        # worker must resume explicitly before another conclusion can replace
        # it.
        if self.stop_reason is not None:
            return self
        return replace(self, version=self.version + 1, stop_reason=reason)

    def resume(self) -> InvestigationWorkingState:
        if self.stop_reason is None:
            return self
        return replace(self, version=self.version + 1, stop_reason=None)


def investigation_bridge_attempt(recent_actions: tuple[str, ...], turn_index: int) -> int:
    """Return the next Bridge attempt after an explicit or automatic disposition."""
    prefixes = (
        f"bridge_turn_abandoned:{turn_index}:next_attempt=",
        f"bridge_turn_orphaned:{turn_index}:next_attempt=",
        f"bridge_turn_pre_submission_retry_exhausted:{turn_index}:next_attempt=",
    )
    attempts = []
    for action in recent_actions:
        for prefix in prefixes:
            if action.startswith(prefix):
                raw = action[len(prefix) :].split(":turn_id=", 1)[0]
                if raw.isdecimal():
                    attempts.append(int(raw))
                break
    return max(attempts, default=0)


def investigation_abandoned_turn_id(
    recent_actions: tuple[str, ...], turn_index: int, next_attempt: int
) -> UUID | None:
    prefix = f"bridge_turn_abandoned:{turn_index}:next_attempt={next_attempt}:turn_id="
    for action in reversed(recent_actions):
        if action.startswith(prefix):
            try:
                return UUID(action.removeprefix(prefix))
            except ValueError:
                return None
    return None


def investigation_orphaned_turn_id(
    recent_actions: tuple[str, ...], turn_index: int, next_attempt: int
) -> UUID | None:
    prefix = f"bridge_turn_orphaned:{turn_index}:next_attempt={next_attempt}:turn_id="
    for action in reversed(recent_actions):
        if action.startswith(prefix):
            try:
                return UUID(action.removeprefix(prefix))
            except ValueError:
                return None
    return None


__all__ = [
    "Hypothesis",
    "HypothesisStatus",
    "InvestigationWorkingState",
    "StopReason",
    "investigation_abandoned_turn_id",
    "investigation_bridge_attempt",
    "investigation_orphaned_turn_id",
]

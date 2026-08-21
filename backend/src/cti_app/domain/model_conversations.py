from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.model_runs import ModelProvider


class ConversationTransport(StrEnum):
    CHATGPT_BRIDGE = "chatgpt_bridge"
    OPENAI_RESPONSES = "openai_responses"
    APPLICATION_MANAGED = "application_managed"


class ConversationPurpose(StrEnum):
    DISCOVERY = "discovery"
    ANALYST_ASSISTANCE = "analyst_assistance"
    PIVOT_RESEARCH = "pivot_research"
    DRAFTING = "drafting"
    CRITIC = "critic"
    SUBJECT_PRODUCTION = "subject_production"


class ConversationStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    BUSY = "busy"
    NEEDS_REVIEW = "needs_review"
    UNAVAILABLE = "unavailable"
    ARCHIVED = "archived"


class ConversationMode(StrEnum):
    FRESH = "fresh"
    CONTINUE = "continue"
    # Reserved by the domain contract; no API or transport accepts it yet.
    FORK = "fork"


class ConversationPolicy(StrEnum):
    """Lifecycle policy for a conversation after successful completion.

    This is a first-order control data, not metadata. Every new conversation
    must define its policy explicitly.
    """
    KEEP = "keep"
    DELETE_ON_SUCCESS = "delete_on_success"


class ConversationReleaseOutcome(StrEnum):
    """Outcome of an explicit conversation release by the client.

    Only SUCCESS may trigger automatic cleanup according to policy.
    All other outcomes preserve the conversation for potential recovery.
    """
    SUCCESS = "success"
    FAILURE = "failure"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


class ConversationLifecycleStatus(StrEnum):
    """State machine for conversation lifecycle.

    ACTIVE: conversation is available for normal use
    RELEASED: client has called release() with an outcome
    DELETE_PENDING: outcome was SUCCESS and policy was DELETE_ON_SUCCESS
    DELETING: cleanup is in progress
    DELETED: conversation has been removed from ChatGPT
    CLEANUP_FAILED: cleanup attempted but failed (retryable)
    RETAINED: conversation is permanently retained (KEEP policy or non-SUCCESS outcome)
    """
    ACTIVE = "active"
    RELEASED = "released"
    DELETE_PENDING = "delete_pending"
    DELETING = "deleting"
    DELETED = "deleted"
    CLEANUP_FAILED = "cleanup_failed"
    RETAINED = "retained"


class ConversationTurnStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


CONTINUABLE_PURPOSES = {
    ConversationPurpose.ANALYST_ASSISTANCE,
    ConversationPurpose.PIVOT_RESEARCH,
    ConversationPurpose.SUBJECT_PRODUCTION,
}


@dataclass(slots=True, kw_only=True)
class ModelConversation:
    provider: ModelProvider
    transport: ConversationTransport
    purpose: ConversationPurpose
    title: str
    edition_id: UUID | None = None
    subject_id: UUID | None = None
    status: ConversationStatus = ConversationStatus.PENDING
    external_id: str | None = None
    external_locator: str | None = None
    expected_profile: str | None = None
    requested_model: str | None = None
    head_turn_id: UUID | None = None
    turn_count: int = 0
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        self.title = " ".join(self.title.split())
        if not self.title:
            raise ValueError("Conversation title is required")
        if self.turn_count < 0 or self.version < 1:
            raise ValueError("Conversation counters are invalid")

    def start_turn(self, *, mode: ConversationMode, now: datetime | None = None) -> None:
        if mode is ConversationMode.FORK:
            raise ValueError("Conversation fork mode is not implemented")
        if self.status is ConversationStatus.ARCHIVED:
            raise ValueError("Conversation is archived")
        if self.status is ConversationStatus.BUSY:
            raise ValueError("conversation_busy")
        if mode is ConversationMode.CONTINUE:
            if self.purpose not in CONTINUABLE_PURPOSES:
                raise ValueError("Conversation purpose requires fresh mode")
            if not self.external_locator or not self.head_turn_id:
                raise ValueError("Conversation has no verified head to continue")
        self.status = ConversationStatus.BUSY
        self.turn_count += 1
        self.updated_at = now or datetime.now(UTC)
        self.version += 1

    def finish_turn(
        self,
        turn_id: UUID,
        *,
        external_locator: str | None,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        self.status = ConversationStatus.READY
        self.external_locator = external_locator or self.external_locator
        self.head_turn_id = turn_id
        self.last_used_at = timestamp
        self.updated_at = timestamp
        self.version += 1

    def mark_problem(self, *, uncertain: bool, now: datetime | None = None) -> None:
        self.status = (
            ConversationStatus.NEEDS_REVIEW if uncertain else ConversationStatus.UNAVAILABLE
        )
        self.updated_at = now or datetime.now(UTC)
        self.version += 1

    def archive(self, now: datetime | None = None) -> None:
        if self.status is ConversationStatus.BUSY:
            raise ValueError("conversation_busy")
        self.status = ConversationStatus.ARCHIVED
        self.updated_at = now or datetime.now(UTC)
        self.version += 1


@dataclass(slots=True, kw_only=True)
class ModelConversationTurn:
    conversation_id: UUID
    sequence: int
    model_run_id: UUID
    input_blob_reference: str
    input_sha256: str
    idempotency_key: str
    parent_turn_id: UUID | None = None
    output_blob_reference: str | None = None
    output_sha256: str | None = None
    status: ConversationTurnStatus = ConversationTurnStatus.RUNNING
    external_turn_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    correlation_id: str = "-"
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.idempotency_key or not self.correlation_id:
            raise ValueError("Turn sequence and idempotency key are required")
        for value in (self.input_sha256, self.output_sha256):
            if value is not None and (
                len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError("Turn hashes must be lowercase SHA-256")

    def succeed(
        self,
        *,
        output_blob_reference: str,
        output_sha256: str,
        external_turn_id: str | None,
        now: datetime | None = None,
    ) -> None:
        if self.status is not ConversationTurnStatus.RUNNING:
            raise ValueError("Conversation turn is already terminal")
        self.output_blob_reference = output_blob_reference
        self.output_sha256 = output_sha256
        self.external_turn_id = external_turn_id
        self.status = ConversationTurnStatus.SUCCEEDED
        self.finished_at = now or datetime.now(UTC)

    def fail(
        self,
        *,
        code: str,
        message: str,
        uncertain: bool,
        blocked: bool = False,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        if self.status is not ConversationTurnStatus.RUNNING:
            return
        self.status = (
            ConversationTurnStatus.BLOCKED
            if blocked
            else ConversationTurnStatus.NEEDS_REVIEW
            if uncertain
            else ConversationTurnStatus.FAILED
        )
        self.error_code = code[:64]
        self.error_message = " ".join(message.replace("\x00", "").split())[:500]
        self.error_details = details
        self.finished_at = now or datetime.now(UTC)


@dataclass(slots=True, kw_only=True)
class ConversationLifecycle:
    """Lifecycle policy and status of a conversation.

    This is a separate concern from ConversationContext (usage: fresh/continue).
    The policy is immutable once set at fresh time.
    """
    policy: ConversationPolicy
    status: ConversationLifecycleStatus = ConversationLifecycleStatus.ACTIVE
    released_at: datetime | None = None
    release_outcome: ConversationReleaseOutcome | None = None
    deleted_at: datetime | None = None
    cleanup_attempt_count: int = 0
    last_cleanup_attempt_at: datetime | None = None
    last_cleanup_error_code: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: int = 1

    def __post_init__(self) -> None:
        if self.status != ConversationLifecycleStatus.ACTIVE and self.release_outcome is None:
            raise ValueError("Non-ACTIVE status requires a release_outcome")
        if self.status == ConversationLifecycleStatus.ACTIVE and self.release_outcome is not None:
            raise ValueError("ACTIVE status must have no release_outcome")

    def release(
        self,
        *,
        outcome: ConversationReleaseOutcome,
        now: datetime | None = None,
    ) -> None:
        """Release the conversation with an explicit outcome.

        The decision to release belongs to the workflow client, not the bridge.
        Only SUCCESS may trigger automatic cleanup according to policy.
        """
        if self.status != ConversationLifecycleStatus.ACTIVE:
            # Idempotence: releasing again is a no-op
            return

        timestamp = now or datetime.now(UTC)
        self.release_outcome = outcome
        self.released_at = timestamp
        self.updated_at = timestamp
        self.version += 1

        # State machine transition based on outcome and policy
        if outcome == ConversationReleaseOutcome.SUCCESS:
            if self.policy == ConversationPolicy.DELETE_ON_SUCCESS:
                self.status = ConversationLifecycleStatus.DELETE_PENDING
            else:  # KEEP
                self.status = ConversationLifecycleStatus.RETAINED
        else:
            # FAILURE, NEEDS_REVIEW, CANCELLED all preserve the conversation
            self.status = ConversationLifecycleStatus.RETAINED

    #: States a cleanup attempt may act on. CLEANUP_FAILED belongs here: the
    #: repository lists those rows precisely so they can be retried, and leaving
    #: it out stranded every conversation whose first deletion attempt failed.
    _RETRYABLE_CLEANUP_STATES = frozenset(
        {
            ConversationLifecycleStatus.DELETE_PENDING,
            ConversationLifecycleStatus.DELETING,
            ConversationLifecycleStatus.CLEANUP_FAILED,
        }
    )

    def start_cleanup(self, *, now: datetime | None = None) -> None:
        """Transition to DELETING state (cleanup in progress)."""
        if self.status not in {
            ConversationLifecycleStatus.DELETE_PENDING,
            ConversationLifecycleStatus.CLEANUP_FAILED,
        }:
            raise ValueError(
                f"Cannot start cleanup from status {self.status.value}"
            )
        timestamp = now or datetime.now(UTC)
        self.status = ConversationLifecycleStatus.DELETING
        self.updated_at = timestamp
        self.version += 1

    def mark_cleanup_failed(
        self,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> None:
        """Mark cleanup attempt as failed (retryable)."""
        if self.status not in self._RETRYABLE_CLEANUP_STATES:
            # Idempotence: ignore if not in cleanup states
            return

        timestamp = now or datetime.now(UTC)
        self.status = ConversationLifecycleStatus.CLEANUP_FAILED
        self.cleanup_attempt_count += 1
        self.last_cleanup_attempt_at = timestamp
        self.last_cleanup_error_code = error_code[:64]
        self.updated_at = timestamp
        self.version += 1

    def mark_deleted(self, *, now: datetime | None = None) -> None:
        """Mark as deleted after successful cleanup."""
        if self.status not in self._RETRYABLE_CLEANUP_STATES:
            # Idempotence: ignore if not in cleanup states
            return

        timestamp = now or datetime.now(UTC)
        self.status = ConversationLifecycleStatus.DELETED
        self.deleted_at = timestamp
        self.cleanup_attempt_count += 1
        self.last_cleanup_attempt_at = timestamp
        self.updated_at = timestamp
        self.version += 1

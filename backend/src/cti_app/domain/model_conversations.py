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
    SUBJECT_RESEARCH = "subject_research"
    DRAFTING = "drafting"
    CRITIC = "critic"


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
    """Browser-session policy for a bridge conversation. Neither value controls
    ChatGPT history persistence — every fresh bridge conversation is already a
    Temporary Chat, never written to ChatGPT's history in the first place.

    KEEP: retain the live browser Temporary Chat session (exact tab + binding)
    for future continuation.

    DELETE_ON_SUCCESS: close the live browser Temporary Chat session after a
    successful bounded operation. Closing the tab is the entire cleanup — see
    chatgpt-bridge/AGENTS.md, section "Ephemeral conversations".
    """

    KEEP = "keep"
    DELETE_ON_SUCCESS = "delete_on_success"


class ConversationTurnStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


CONTINUABLE_PURPOSES = {
    ConversationPurpose.ANALYST_ASSISTANCE,
    ConversationPurpose.PIVOT_RESEARCH,
    ConversationPurpose.SUBJECT_RESEARCH,
    ConversationPurpose.DRAFTING,
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
            if not self.head_turn_id:
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

    def adopt_recovery_output(
        self,
        *,
        output_blob_reference: str,
        output_sha256: str,
        external_turn_id: str | None,
        now: datetime | None = None,
    ) -> None:
        """Close a turn from an already-adopted exact ModelRun output."""
        if self.status not in {ConversationTurnStatus.RUNNING, ConversationTurnStatus.NEEDS_REVIEW}:
            raise ValueError("Conversation turn is not recoverable")
        self.output_blob_reference = output_blob_reference
        self.output_sha256 = output_sha256
        self.external_turn_id = external_turn_id
        self.status = ConversationTurnStatus.SUCCEEDED
        self.error_code = None
        self.error_message = None
        self.error_details = None
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

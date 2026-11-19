"""Pure identities and append-only timeline values for editorial threads."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EditorialThreadError(ValueError):
    code = "editorial_thread_error"


class EditorialThreadConflict(EditorialThreadError):
    code = "idempotency_conflict"


class EditorialThreadEventAppendOnlyError(EditorialThreadError):
    code = "editorial_thread_event_append_only"


class EditorialBindingState(StrEnum):
    VALID = "valid"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"


class EditorialBridgeStatus(StrEnum):
    READY = "ready"
    WAITING_FOR_CAPACITY = "waiting_for_capacity"
    GENERATING = "generating"
    REPLY_AVAILABLE = "reply_available"
    REFUSED = "refused"
    SESSION_LOST = "session_lost"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class EditorialThreadEventKind(StrEnum):
    USER_MESSAGE = "user_message"
    BRIDGE_REPLY = "bridge_reply"
    PROPOSAL = "proposal"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REFUSED = "refused"
    RECONCILIATION = "reconciliation"
    GENERATION = "generation"


class EditorialThreadJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class EditorialThreadBinding:
    subject_id: UUID
    production_run_id: UUID
    occurrence_id: UUID
    artifact_id: UUID
    artifact_version: int
    canonical_sha256: str

    def __post_init__(self) -> None:
        if self.artifact_version < 1:
            raise ValueError("editorial_thread_artifact_version_invalid")
        if not _SHA256.fullmatch(self.canonical_sha256):
            raise ValueError("editorial_thread_canonical_sha256_invalid")


@dataclass(frozen=True, slots=True)
class EditorialThreadEvent:
    id: UUID
    thread_id: UUID
    sequence: int
    kind: EditorialThreadEventKind
    actor_id: UUID | None
    idempotency_key: str
    request_sha256: str
    payload_blob_ref: UUID | None = None
    payload_sha256: str | None = None
    proposal_id: UUID | None = None
    model_turn_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.idempotency_key:
            raise ValueError("editorial_thread_event_identity_invalid")
        if not _SHA256.fullmatch(self.request_sha256):
            raise ValueError("editorial_thread_request_sha256_invalid")
        if (self.payload_blob_ref is None) != (self.payload_sha256 is None):
            raise ValueError("editorial_thread_payload_reference_invalid")
        if self.payload_sha256 is not None and not _SHA256.fullmatch(self.payload_sha256):
            raise ValueError("editorial_thread_payload_sha256_invalid")


@dataclass(frozen=True, slots=True)
class EditorialThread:
    id: UUID
    origin: EditorialThreadBinding
    model_conversation_id: UUID | None
    bridge_status: EditorialBridgeStatus
    next_sequence: int
    version: int
    session_number: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.next_sequence < 1 or self.version < 1 or self.session_number < 1:
            raise ValueError("editorial_thread_counters_invalid")

    def start_new_session(
        self,
        *,
        origin: EditorialThreadBinding,
        model_conversation_id: UUID | None,
        now: datetime | None = None,
    ) -> EditorialThread:
        """Start the next per-occurrence session only after loss was recorded."""
        if self.bridge_status is not EditorialBridgeStatus.SESSION_LOST:
            raise EditorialThreadError("editorial_thread_session_not_lost")
        if (
            origin.subject_id != self.origin.subject_id
            or origin.production_run_id != self.origin.production_run_id
            or origin.occurrence_id != self.origin.occurrence_id
        ):
            raise EditorialThreadError("editorial_thread_session_scope_mismatch")
        timestamp = now or datetime.now(UTC)
        return EditorialThread(
            id=uuid4(),
            origin=origin,
            model_conversation_id=model_conversation_id,
            bridge_status=EditorialBridgeStatus.READY,
            next_sequence=1,
            version=1,
            session_number=self.session_number + 1,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def append_event(
        self,
        *,
        kind: EditorialThreadEventKind,
        actor_id: UUID | None,
        idempotency_key: str,
        request_sha256: str,
        payload_blob_ref: UUID | None = None,
        payload_sha256: str | None = None,
        proposal_id: UUID | None = None,
        model_turn_id: UUID | None = None,
        existing_event: EditorialThreadEvent | None = None,
        now: datetime | None = None,
    ) -> tuple[EditorialThread, EditorialThreadEvent]:
        """Return the next immutable event and thread counters for one append."""
        if existing_event is not None:
            if (
                existing_event.idempotency_key == idempotency_key
                and existing_event.request_sha256 == request_sha256
            ):
                return self, existing_event
            raise EditorialThreadConflict("idempotency_conflict")
        timestamp = now or datetime.now(UTC)
        event = EditorialThreadEvent(
            id=uuid4(),
            thread_id=self.id,
            sequence=self.next_sequence,
            kind=kind,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            payload_blob_ref=payload_blob_ref,
            payload_sha256=payload_sha256,
            proposal_id=proposal_id,
            model_turn_id=model_turn_id,
            created_at=timestamp,
        )
        return (
            replace(
                self,
                next_sequence=self.next_sequence + 1,
                version=self.version + 1,
                updated_at=timestamp,
            ),
            event,
        )


@dataclass(frozen=True, slots=True)
class EditorialThreadRecord:
    thread: EditorialThread
    events: tuple[EditorialThreadEvent, ...]

"""Typed state for the single editorial Bridge generation lease."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class BridgeGenerationSlotState(StrEnum):
    FREE = "free"
    HELD = "held"
    HELD_UNKNOWN = "held_unknown"


class BridgeGenerationPreviousOutcome(StrEnum):
    TERMINAL = "terminal"
    NOT_SUBMITTED = "not_submitted"
    UNKNOWN = "unknown"


class BridgeGenerationSlotAcquireResult(StrEnum):
    ACQUIRED = "acquired"
    REENTERED = "reentered"
    BUSY = "busy"
    HELD_UNKNOWN = "held_unknown"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class BridgeGenerationSlot:
    singleton_key: int
    holder_thread_id: UUID | None
    holder_job_id: UUID | None
    holder_turn_id: UUID | None
    lease_token: int
    acquired_at: datetime | None
    lease_expires_at: datetime | None
    state: BridgeGenerationSlotState

    def __post_init__(self) -> None:
        if self.singleton_key != 1:
            raise ValueError("bridge_generation_slot_key_invalid")
        if self.lease_token < 0:
            raise ValueError("bridge_generation_slot_token_invalid")
        if self.state is BridgeGenerationSlotState.FREE:
            if any(
                value is not None
                for value in (
                    self.holder_thread_id,
                    self.holder_job_id,
                    self.holder_turn_id,
                    self.acquired_at,
                    self.lease_expires_at,
                )
            ):
                raise ValueError("free_bridge_generation_slot_has_holder")
        elif (
            self.holder_thread_id is None
            or self.holder_job_id is None
            or self.acquired_at is None
            or self.lease_expires_at is None
        ):
            raise ValueError("held_bridge_generation_slot_missing_holder")


@dataclass(frozen=True, slots=True)
class EditorialBridgeGenerationLease:
    thread_id: UUID
    job_id: UUID
    worker_lease_token: UUID
    turn_idempotency_key: str
    lease_token: int

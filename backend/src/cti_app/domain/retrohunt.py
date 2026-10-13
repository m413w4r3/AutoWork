"""Durable subject-scoped Retrohunt reservations and provider contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT = 2


class RetrohuntReservationState(StrEnum):
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class RetrohuntProviderJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RetrohuntProviderErrorCode(StrEnum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrohuntReservation:
    subject_id: UUID
    investigation_id: UUID
    slot_number: int
    operation_key: str
    rule_version_id: UUID
    rule_source_sha256: str
    corpus_id: str
    corpus_sha256: str
    parameters: Mapping[str, Any]
    state: RetrohuntReservationState = RetrohuntReservationState.RESERVED
    submission_token: UUID | None = None
    provider_job_id: str | None = None
    version: int = 1
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", RetrohuntReservationState(self.state))
        if not 1 <= self.slot_number <= RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT:
            raise ValueError("Retrohunt slot is outside the subject submission limit")
        if not _SHA256_RE.fullmatch(self.operation_key):
            raise ValueError("operation_key must be a lowercase SHA-256")
        for name, value in (
            ("rule_source_sha256", self.rule_source_sha256),
            ("corpus_sha256", self.corpus_sha256),
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if not self.corpus_id.strip():
            raise ValueError("corpus_id cannot be empty")
        if self.version < 1:
            raise ValueError("version must be positive")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("reservation timestamps must be timezone-aware")

    def transition(
        self,
        state: RetrohuntReservationState,
        *,
        submission_token: UUID | None = None,
        provider_job_id: str | None = None,
        now: datetime | None = None,
    ) -> RetrohuntReservation:
        target = RetrohuntReservationState(state)
        allowed = {
            RetrohuntReservationState.RESERVED: {
                RetrohuntReservationState.SUBMITTING,
                RetrohuntReservationState.RECONCILIATION_REQUIRED,
            },
            RetrohuntReservationState.SUBMITTING: {
                RetrohuntReservationState.SUBMITTED,
                RetrohuntReservationState.COMPLETED,
                RetrohuntReservationState.FAILED,
                RetrohuntReservationState.RECONCILIATION_REQUIRED,
            },
            RetrohuntReservationState.SUBMITTED: {
                RetrohuntReservationState.COMPLETED,
                RetrohuntReservationState.FAILED,
                RetrohuntReservationState.RECONCILIATION_REQUIRED,
            },
            RetrohuntReservationState.RECONCILIATION_REQUIRED: {
                RetrohuntReservationState.SUBMITTED,
                RetrohuntReservationState.COMPLETED,
                RetrohuntReservationState.FAILED,
            },
            RetrohuntReservationState.COMPLETED: set(),
            RetrohuntReservationState.FAILED: set(),
        }
        if target not in allowed[self.state]:
            raise ValueError(f"Invalid Retrohunt transition: {self.state.value} -> {target.value}")
        return replace(
            self,
            state=target,
            submission_token=(
                self.submission_token if submission_token is None else submission_token
            ),
            provider_job_id=(self.provider_job_id if provider_job_id is None else provider_job_id),
            version=self.version + 1,
            updated_at=now or datetime.now(UTC),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrohuntSubmitRequest:
    subject_id: UUID
    investigation_id: UUID
    reservation_id: UUID
    slot_number: int
    rule_version_id: UUID
    rule_source: str
    rule_source_sha256: str
    corpus_id: str
    corpus_sha256: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrohuntProviderJob:
    job_id: str
    state: RetrohuntProviderJobState

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("provider job_id cannot be empty")
        object.__setattr__(self, "state", RetrohuntProviderJobState(self.state))


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrohuntHit:
    sha256: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("Retrohunt hits must identify a lowercase SHA-256")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrohuntProviderResults:
    job_id: str
    hits: tuple[RetrohuntHit, ...]

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("provider job_id cannot be empty")
        hits = tuple(self.hits)
        if len({hit.sha256 for hit in hits}) != len(hits):
            raise ValueError("Retrohunt results cannot contain duplicate hits")
        object.__setattr__(self, "hits", hits)

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class InvalidJobTransitionError(ValueError):
    pass


@dataclass(slots=True)
class Job:
    kind: str
    aggregate_type: str
    aggregate_id: UUID
    idempotency_key: str
    correlation_id: str
    input_parameters: dict[str, Any]
    max_attempts: int = 3
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = JobStatus.QUEUED
    progress_current: int = 0
    progress_total: int = 0
    user_message: str | None = None
    attempt: int = 0
    next_retry_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    output_reference: str | None = None
    cancellation_requested_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("Job kind must not be empty")
        if not self.aggregate_type.strip():
            raise ValueError("Aggregate type must not be empty")
        if not self.idempotency_key.strip():
            raise ValueError("Idempotency key must not be empty")
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        self._validate_progress(self.progress_current, self.progress_total)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES

    @property
    def cancellation_requested(self) -> bool:
        return self.cancellation_requested_at is not None

    def start(self, now: datetime | None = None) -> None:
        self._require_status(JobStatus.QUEUED)
        if self.attempt >= self.max_attempts:
            raise InvalidJobTransitionError("Job has exhausted its attempts")
        timestamp = now or datetime.now(UTC)
        self.status = JobStatus.RUNNING
        self.attempt += 1
        self.started_at = self.started_at or timestamp
        self.finished_at = None
        self.heartbeat_at = timestamp
        self.next_retry_at = None
        self.error_code = None
        self.error_message = None
        self.error_details = None
        self.updated_at = timestamp

    def report_progress(
        self,
        current: int,
        total: int,
        message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self._require_status(JobStatus.RUNNING)
        self._validate_progress(current, total)
        timestamp = now or datetime.now(UTC)
        self.progress_current = current
        self.progress_total = total
        self.user_message = message
        self.heartbeat_at = timestamp
        self.updated_at = timestamp

    def record_diagnostics(
        self,
        details: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        self._require_status(JobStatus.RUNNING)
        timestamp = now or datetime.now(UTC)
        self.error_details = details
        self.heartbeat_at = timestamp
        self.updated_at = timestamp

    def wait_for_human(self, message: str, now: datetime | None = None) -> None:
        self._require_status(JobStatus.RUNNING)
        timestamp = now or datetime.now(UTC)
        self.status = JobStatus.WAITING_HUMAN
        self.user_message = message
        self.heartbeat_at = timestamp
        self.updated_at = timestamp

    def succeed(self, output_reference: str | None, now: datetime | None = None) -> None:
        self._require_status(JobStatus.RUNNING)
        timestamp = now or datetime.now(UTC)
        self.status = JobStatus.SUCCEEDED
        self.output_reference = output_reference
        self.finished_at = timestamp
        self.heartbeat_at = timestamp
        self.error_code = None
        self.error_message = None
        self.error_details = None
        self.updated_at = timestamp

    def fail(
        self,
        code: str,
        message: str,
        now: datetime | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._require_status(JobStatus.RUNNING)
        timestamp = now or datetime.now(UTC)
        self.status = JobStatus.FAILED
        self.error_code = code
        self.error_message = message
        self.error_details = details
        self.finished_at = timestamp
        self.heartbeat_at = timestamp
        self.next_retry_at = None
        self.updated_at = timestamp

    def schedule_retry(
        self,
        code: str,
        message: str,
        delay: timedelta,
        now: datetime | None = None,
    ) -> None:
        self._require_status(JobStatus.RUNNING)
        if self.attempt >= self.max_attempts:
            raise InvalidJobTransitionError("Job has exhausted its attempts")
        timestamp = now or datetime.now(UTC)
        self.status = JobStatus.QUEUED
        self.error_code = code
        self.error_message = message
        self.error_details = None
        self.user_message = "Nouvelle tentative planifiée"
        self.next_retry_at = timestamp + delay
        self.heartbeat_at = timestamp
        self.updated_at = timestamp

    def retry_manually(self, now: datetime | None = None) -> None:
        if self.status not in {JobStatus.FAILED, JobStatus.WAITING_HUMAN}:
            raise InvalidJobTransitionError(f"Cannot retry a job in status {self.status.value}")
        if self.attempt >= self.max_attempts:
            raise InvalidJobTransitionError("Job has exhausted its attempts")
        timestamp = now or datetime.now(UTC)
        self.status = JobStatus.QUEUED
        self.next_retry_at = None
        self.finished_at = None
        self.cancellation_requested_at = None
        self.error_code = None
        self.error_message = None
        self.error_details = None
        self.user_message = "Relance demandée"
        self.updated_at = timestamp

    def request_cancellation(self, now: datetime | None = None) -> None:
        if self.is_terminal:
            raise InvalidJobTransitionError(f"Cannot cancel a job in status {self.status.value}")
        timestamp = now or datetime.now(UTC)
        self.cancellation_requested_at = timestamp
        self.updated_at = timestamp
        if self.status in {JobStatus.QUEUED, JobStatus.WAITING_HUMAN}:
            self.mark_cancelled(timestamp)
        else:
            self.user_message = "Annulation demandée"

    def mark_cancelled(self, now: datetime | None = None) -> None:
        if self.status not in {
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.WAITING_HUMAN,
        }:
            raise InvalidJobTransitionError(
                f"Cannot mark a job in status {self.status.value} as cancelled"
            )
        timestamp = now or datetime.now(UTC)
        self.status = JobStatus.CANCELLED
        self.cancellation_requested_at = self.cancellation_requested_at or timestamp
        self.user_message = "Tâche annulée"
        self.finished_at = timestamp
        self.heartbeat_at = timestamp
        self.next_retry_at = None
        self.updated_at = timestamp

    def recover_abandoned(
        self,
        now: datetime | None = None,
        *,
        resume_current_attempt: bool = False,
    ) -> None:
        self._require_status(JobStatus.RUNNING)
        timestamp = now or datetime.now(UTC)
        if self.cancellation_requested:
            self.mark_cancelled(timestamp)
        elif resume_current_attempt:
            # A durable external run survives its worker. Requeueing resumes
            # the same business attempt; `start()` will restore this counter.
            self.status = JobStatus.QUEUED
            self.attempt = max(0, self.attempt - 1)
            self.next_retry_at = timestamp
            self.error_code = "worker_interrupted"
            self.error_message = "La tâche reprend son attente durable après une interruption."
            self.user_message = "Reprise de l'attente planifiée"
            self.updated_at = timestamp
        elif self.attempt < self.max_attempts:
            self.status = JobStatus.QUEUED
            self.next_retry_at = timestamp
            self.error_code = "heartbeat_expired"
            self.error_message = "La tâche a été reprise après une interruption du worker."
            self.user_message = "Reprise planifiée"
            self.updated_at = timestamp
        else:
            self.fail(
                "heartbeat_expired",
                "La tâche a été interrompue et ne peut plus être relancée automatiquement.",
                timestamp,
            )

    def _require_status(self, expected: JobStatus) -> None:
        if self.status is not expected:
            raise InvalidJobTransitionError(f"Expected {expected.value}, got {self.status.value}")

    @staticmethod
    def _validate_progress(current: int, total: int) -> None:
        if current < 0 or total < 0 or (total > 0 and current > total):
            raise ValueError("Invalid job progress")


@dataclass(frozen=True, slots=True)
class JobEvent:
    job_id: UUID
    event_type: str
    from_status: JobStatus | None
    to_status: JobStatus
    actor_id: str
    correlation_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class JobOperationalMetrics:
    total: int
    counts_by_status: dict[JobStatus, int]
    retry_waiting: int
    average_duration_seconds: float | None
    failure_rate: float

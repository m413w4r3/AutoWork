from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

JOB_STATUS_VALUES_SQL = "'queued', 'running', 'waiting_human', 'succeeded', 'failed', 'cancelled'"


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(f"status IN ({JOB_STATUS_VALUES_SQL})", name="ck_jobs_status"),
        CheckConstraint("progress_current >= 0", name="ck_jobs_progress_current"),
        CheckConstraint("progress_total >= 0", name="ck_jobs_progress_total"),
        CheckConstraint(
            "progress_total = 0 OR progress_current <= progress_total",
            name="ck_jobs_progress_bounds",
        ),
        CheckConstraint("attempt >= 0", name="ck_jobs_attempt"),
        CheckConstraint("max_attempts BETWEEN 1 AND 20", name="ck_jobs_max_attempts"),
        Index("ix_jobs_status_next_retry", "status", "next_retry_at"),
        Index("ix_jobs_running_heartbeat", "status", "heartbeat_at"),
        Index("ix_jobs_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    progress_current: Mapped[int] = mapped_column(BigInteger, nullable=False)
    progress_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    attempt: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_attempts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_reference: Mapped[str | None] = mapped_column(Text)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobEventRow(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        CheckConstraint(f"to_status IN ({JOB_STATUS_VALUES_SQL})", name="ck_job_events_to_status"),
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({JOB_STATUS_VALUES_SQL})",
            name="ck_job_events_from_status",
        ),
        Index("ix_job_events_job", "job_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

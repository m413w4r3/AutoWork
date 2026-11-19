from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

EDITORIAL_THREAD_BRIDGE_STATES_SQL = (
    "'ready', 'waiting_for_capacity', 'generating', 'reply_available', 'refused', "
    "'session_lost', 'reconciliation_required'"
)
EDITORIAL_THREAD_EVENT_KINDS_SQL = (
    "'user_message', 'bridge_reply', 'proposal', 'accepted', 'rejected', 'refused', "
    "'reconciliation', 'generation'"
)
EDITORIAL_THREAD_JOB_STATES_SQL = (
    "'queued', 'waiting_for_capacity', 'running', 'reconciliation_required', "
    "'complete', 'failed', 'cancelled'"
)


class EditorialThreadRow(Base):
    __tablename__ = "editorial_threads"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "occurrence_id",
            "session_number",
            name="uq_editorial_threads_subject_occurrence_session",
        ),
        UniqueConstraint("model_conversation_id", name="uq_editorial_threads_model_conversation"),
        CheckConstraint(
            "origin_artifact_version >= 1 AND "
            "char_length(origin_canonical_sha256) = 64 AND "
            "origin_canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_threads_origin_fence",
        ),
        CheckConstraint(
            "generation_state IN (" + EDITORIAL_THREAD_BRIDGE_STATES_SQL + ")",
            name="ck_editorial_threads_generation_state",
        ),
        CheckConstraint(
            "next_sequence >= 1 AND version >= 1",
            name="ck_editorial_threads_counters",
        ),
        CheckConstraint("session_number >= 1", name="ck_editorial_threads_session_number"),
        Index("ix_editorial_threads_subject_occurrence", "subject_id", "occurrence_id"),
        Index(
            "ix_editorial_threads_current_session",
            "subject_id",
            "production_run_id",
            "occurrence_id",
            "session_number",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("subjects.id", name="fk_editorial_threads_subject", ondelete="RESTRICT"),
        nullable=False,
    )
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_runs.id", name="fk_editorial_threads_run", ondelete="RESTRICT"),
        nullable=False,
    )
    occurrence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    session_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_threads_origin_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    origin_artifact_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_conversation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "model_conversations.id",
            name="fk_editorial_threads_model_conversation",
            ondelete="RESTRICT",
        ),
    )
    generation_state: Mapped[str] = mapped_column(String(32), nullable=False)
    next_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EditorialThreadEventRow(Base):
    __tablename__ = "editorial_thread_events"
    __table_args__ = (
        UniqueConstraint("thread_id", "sequence", name="uq_editorial_thread_events_sequence"),
        UniqueConstraint(
            "thread_id", "idempotency_key", name="uq_editorial_thread_events_idempotency"
        ),
        CheckConstraint("sequence >= 1", name="ck_editorial_thread_events_sequence"),
        CheckConstraint(
            "event_kind IN (" + EDITORIAL_THREAD_EVENT_KINDS_SQL + ")",
            name="ck_editorial_thread_events_kind",
        ),
        CheckConstraint(
            "char_length(request_sha256) = 64 AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_thread_events_request_hash",
        ),
        CheckConstraint(
            "payload_blob_ref IS NULL AND payload_sha256 IS NULL OR "
            "payload_blob_ref IS NOT NULL AND payload_sha256 IS NOT NULL AND "
            "char_length(payload_sha256) = 64 AND payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_thread_events_payload_ref",
        ),
        Index("ix_editorial_thread_events_timeline", "thread_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    thread_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_threads.id",
            name="fk_editorial_thread_events_thread",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_blob_ref: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("blobs.id", name="fk_editorial_thread_events_payload_blob", ondelete="RESTRICT"),
    )
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    proposal_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    model_turn_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "model_conversation_turns.id",
            name="fk_editorial_thread_events_model_turn",
            ondelete="RESTRICT",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EditorialThreadJobRow(Base):
    __tablename__ = "editorial_thread_jobs"
    __table_args__ = (
        UniqueConstraint("send_event_id", name="uq_editorial_thread_jobs_send_event"),
        CheckConstraint(
            "state IN (" + EDITORIAL_THREAD_JOB_STATES_SQL + ")",
            name="ck_editorial_thread_jobs_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_editorial_thread_jobs_attempt_count"),
        Index(
            "uq_editorial_thread_jobs_active_thread",
            "thread_id",
            unique=True,
            postgresql_where=text(
                "state IN ('queued', 'waiting_for_capacity', 'running', 'reconciliation_required')"
            ),
        ),
        Index("ix_editorial_thread_jobs_lease", "state", "lease_until"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    thread_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_threads.id",
            name="fk_editorial_thread_jobs_thread",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    send_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_thread_events.id",
            name="fk_editorial_thread_jobs_send_event",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

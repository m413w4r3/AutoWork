from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TLP_VALUES_SQL = "'CLEAR', 'GREEN', 'AMBER', 'AMBER+STRICT', 'RED'"
JOB_STATUS_VALUES_SQL = "'queued', 'running', 'waiting_human', 'succeeded', 'failed', 'cancelled'"
EDITION_STATUS_VALUES_SQL = (
    "'draft', 'discovery', 'selection', 'production', 'review', "
    "'assembling', 'published', 'archived'"
)
MODEL_PROVIDER_VALUES_SQL = "'openai', 'qwen', 'fake'"
MODEL_ROLE_VALUES_SQL = "'research', 'structured_extraction', 'drafting', 'critic'"
MODEL_RUN_STATUS_VALUES_SQL = "'running', 'waiting_background', 'succeeded', 'failed', 'blocked'"


class Base(DeclarativeBase):
    pass


class BlobRow(Base):
    __tablename__ = "blobs"
    __table_args__ = (
        UniqueConstraint("logical_bucket", "sha256", name="uq_blobs_bucket_sha256"),
        UniqueConstraint("object_key", name="uq_blobs_object_key"),
        CheckConstraint("size >= 0", name="ck_blobs_size_non_negative"),
        CheckConstraint("char_length(sha256) = 64", name="ck_blobs_sha256_length"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_blobs_sha256_format"),
        CheckConstraint(
            "logical_bucket ~ '^[a-z0-9][a-z0-9._-]{0,62}$'",
            name="ck_blobs_logical_bucket_format",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    logical_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    object_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubjectRow(Base):
    __tablename__ = "subjects"
    __table_args__ = (
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_subjects_tlp"),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_subjects_slug_format"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceDocumentRow(Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_source_documents_tlp"),
        Index("ix_source_documents_subject_id", "subject_id"),
        Index("ix_source_documents_blob_id", "blob_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    license_restriction: Mapped[str | None] = mapped_column(Text)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    do_not_submit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SampleRow(Base):
    __tablename__ = "samples"
    __table_args__ = (
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_samples_tlp"),
        Index("ix_samples_subject_id", "subject_id"),
        Index("ix_samples_blob_id", "blob_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    license_restriction: Mapped[str | None] = mapped_column(Text)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    do_not_submit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProvenanceEventRow(Base):
    __tablename__ = "provenance_events"
    __table_args__ = (
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_provenance_events_tlp"),
        Index(
            "ix_provenance_events_aggregate",
            "aggregate_type",
            "aggregate_id",
            "occurred_at",
        ),
        Index("ix_provenance_events_subject_id", "subject_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_reference: Mapped[str | None] = mapped_column(Text)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EditionRow(Base):
    __tablename__ = "editions"
    __table_args__ = (
        UniqueConstraint(
            "country_code",
            "period_start",
            "period_end",
            name="uq_editions_country_period",
        ),
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_editions_tlp"),
        CheckConstraint(f"status IN ({EDITION_STATUS_VALUES_SQL})", name="ck_editions_status"),
        CheckConstraint("version >= 1", name="ck_editions_version"),
        CheckConstraint("target_major_articles BETWEEN 0 AND 20", name="ck_editions_major"),
        CheckConstraint("target_briefs BETWEEN 0 AND 100", name="ck_editions_briefs"),
        CheckConstraint("period_start <= period_end", name="ck_editions_period_order"),
        Index("ix_editions_country_status", "country_code", "status"),
        Index("ix_editions_period", "period_start", "period_end"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    country: Mapped[str] = mapped_column(String(100), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    period_start: Mapped[date] = mapped_column(nullable=False)
    period_end: Mapped[date] = mapped_column(nullable=False)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    languages: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    target_major_articles: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_briefs: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_edition_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="SET NULL")
    )
    source_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EditionAuditEventRow(Base):
    __tablename__ = "edition_audit_events"
    __table_args__ = (Index("ix_edition_audit_edition", "edition_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class ModelRunRow(Base):
    __tablename__ = "model_runs"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({MODEL_PROVIDER_VALUES_SQL})", name="ck_model_runs_provider"
        ),
        CheckConstraint(f"model_role IN ({MODEL_ROLE_VALUES_SQL})", name="ck_model_runs_role"),
        CheckConstraint(f"status IN ({MODEL_RUN_STATUS_VALUES_SQL})", name="ck_model_runs_status"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_model_runs_duration"),
        CheckConstraint(
            "char_length(authorized_input_hash) = 64",
            name="ck_model_runs_input_hash_length",
        ),
        CheckConstraint(
            "char_length(evidence_pack_hash) = 64",
            name="ck_model_runs_evidence_hash_length",
        ),
        Index("ix_model_runs_status", "status", "updated_at"),
        Index("ix_model_runs_evidence", "evidence_pack_hash", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_role: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(128), nullable=False)
    actual_model_version: Mapped[str | None] = mapped_column(String(255))
    prompt_template_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    authorized_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    output_references: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

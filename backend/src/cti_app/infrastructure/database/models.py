from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
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
MODEL_RUN_STATUS_VALUES_SQL = (
    "'running', 'waiting_background', 'needs_review', 'succeeded', 'failed', 'blocked'"
)
EDITORIAL_GROUP_STATUS_VALUES_SQL = "'proposed', 'rejected', 'selected', 'superseded'"
GROUPING_OUTCOME_VALUES_SQL = (
    "'new_subject', 'duplicate_same_publication', 'update_previous_subject', "
    "'non_independent_reprint', 'ambiguous_review'"
)
EDITORIAL_TYPE_VALUES_SQL = "'brief', 'major'"
RELATIONSHIP_STATUS_VALUES_SQL = "'provisional', 'verified'"
GROUPING_CONFIDENCE_VALUES_SQL = "'low', 'medium', 'high'"
COLLECTION_STATE_VALUES_SQL = (
    "'pending', 'queued', 'fetching', 'archived', 'extracted', 'completed', 'unavailable', "
    "'blocked', 'failed_retryable', 'failed_terminal'"
)
SOURCE_ROLE_VALUES_SQL = "'primary', 'independent', 'relay', 'aggregator', 'social', 'unknown'"
ATTEMPT_OUTCOME_VALUES_SQL = (
    "'succeeded', 'unavailable', 'blocked', 'too_large', 'error', 'interrupted'"
)
CLAIM_KIND_VALUES_SQL = (
    "'name', 'date', 'ioc', 'cve', 'fact', 'assessment', 'uncertainty', "
    "'infection_chain', 'ttp', 'victimology'"
)
INDICATOR_KIND_VALUES_SQL = "'hash', 'domain', 'ip', 'url', 'cve', 'attack_id', 'email'"
HUMAN_DECISION_VALUES_SQL = (
    "'merge', 'split', 'reject', 'select', 'claim_validate', 'claim_correct', "
    "'claim_reject', 'indicator_validate', 'indicator_correct', 'indicator_reject', "
    "'source_relationship_validate', 'source_relationship_correct', "
    "'brief_changes_requested', 'brief_approve', 'brief_promote'"
)
BRIEF_DRAFT_STATUS_VALUES_SQL = "'draft', 'changes_requested', 'approved', 'promoted'"
CONVERSATION_TRANSPORT_VALUES_SQL = "'chatgpt_bridge', 'openai_responses', 'application_managed'"
CONVERSATION_PURPOSE_VALUES_SQL = (
    "'discovery', 'analyst_assistance', 'pivot_research', "
    "'drafting', 'critic', 'subject_production'"
)
CONVERSATION_STATUS_VALUES_SQL = (
    "'pending', 'ready', 'busy', 'needs_review', 'unavailable', 'archived'"
)
CONVERSATION_TURN_STATUS_VALUES_SQL = "'running', 'succeeded', 'failed', 'needs_review', 'blocked'"
PRODUCTION_PROFILE_VALUES_SQL = "'brief_auto', 'major_assisted'"
PRODUCTION_STATUS_VALUES_SQL = "'queued', 'running', 'ready', 'needs_review', 'failed', 'cancelled'"
PRODUCTION_STAGE_VALUES_SQL = "'sources', 'references', 'extraction', 'synthesis', 'assembly'"
PRODUCTION_ARTIFACT_STAGE_VALUES_SQL = "'references', 'extraction', 'synthesis', 'brief'"
PRODUCTION_ARTIFACT_STATUS_VALUES_SQL = "'verified', 'stale', 'needs_review'"
PRODUCTION_BATCH_STATUS_VALUES_SQL = (
    "'queued', 'running', 'completed', 'completed_with_issues', 'cancelled'"
)


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
    logical_filename: Mapped[str | None] = mapped_column(Text)
    source_collection_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_candidate_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    decoded_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT")
    )
    title: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[date | None] = mapped_column(Date)
    final_url: Mapped[str | None] = mapped_column(Text)
    declared_mime_type: Mapped[str | None] = mapped_column(String(255))
    detected_mime_type: Mapped[str | None] = mapped_column(String(255))
    encoded_sha256: Mapped[str | None] = mapped_column(String(64))
    decoded_sha256: Mapped[str | None] = mapped_column(String(64))
    encoded_size: Mapped[int | None] = mapped_column(BigInteger)
    decoded_size: Mapped[int | None] = mapped_column(BigInteger)
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
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_output_reference: Mapped[str | None] = mapped_column(Text)
    raw_output_sha256: Mapped[str | None] = mapped_column(String(64))
    raw_output_chars: Mapped[int | None] = mapped_column(BigInteger)
    normalized_output_reference: Mapped[str | None] = mapped_column(Text)
    normalized_output_sha256: Mapped[str | None] = mapped_column(String(64))
    parser_stage: Mapped[str | None] = mapped_column(String(64))
    serializer_version: Mapped[str | None] = mapped_column(String(64))
    normalization_version: Mapped[str | None] = mapped_column(String(64))
    json_error_line: Mapped[int | None] = mapped_column(BigInteger)
    json_error_column: Mapped[int | None] = mapped_column(BigInteger)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    transformations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    citation_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    extracted_url_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    visible_citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelOutputRejectionRow(Base):
    __tablename__ = "model_output_rejections"
    __table_args__ = (Index("ix_model_output_rejections_run", "model_run_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    model_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT"), nullable=False
    )
    path: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    value_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_output_reference: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelConversationRow(Base):
    __tablename__ = "model_conversations"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({MODEL_PROVIDER_VALUES_SQL})", name="ck_model_conversations_provider"
        ),
        CheckConstraint(
            f"transport IN ({CONVERSATION_TRANSPORT_VALUES_SQL})",
            name="ck_model_conversations_transport",
        ),
        CheckConstraint(
            f"purpose IN ({CONVERSATION_PURPOSE_VALUES_SQL})", name="ck_model_conversations_purpose"
        ),
        CheckConstraint(
            f"status IN ({CONVERSATION_STATUS_VALUES_SQL})", name="ck_model_conversations_status"
        ),
        CheckConstraint("turn_count >= 0 AND version >= 1", name="ck_model_conversations_counters"),
        Index("ix_model_conversations_subject", "subject_id", "updated_at"),
        Index("ix_model_conversations_edition", "edition_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    edition_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT")
    )
    subject_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    external_locator: Mapped[str | None] = mapped_column(Text)
    expected_profile: Mapped[str | None] = mapped_column(String(255))
    requested_model: Mapped[str | None] = mapped_column(String(255))
    head_turn_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "model_conversation_turns.id",
            name="fk_model_conversations_head_turn",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    turn_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ModelConversationTurnRow(Base):
    __tablename__ = "model_conversation_turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_model_conversation_turn_sequence"),
        UniqueConstraint("model_run_id", name="uq_model_conversation_turn_model_run"),
        UniqueConstraint("idempotency_key", name="uq_model_conversation_turn_idempotency"),
        CheckConstraint(
            f"status IN ({CONVERSATION_TURN_STATUS_VALUES_SQL})",
            name="ck_model_conversation_turns_status",
        ),
        CheckConstraint("sequence >= 1", name="ck_model_conversation_turns_sequence"),
        Index("ix_model_conversation_turns_conversation", "conversation_id", "sequence"),
        Index(
            "uq_model_conversation_turn_running",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("model_conversations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_turn_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_conversation_turns.id", ondelete="RESTRICT")
    )
    model_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT"), nullable=False
    )
    input_blob_reference: Mapped[str] = mapped_column(Text, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_blob_reference: Mapped[str | None] = mapped_column(Text)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    external_turn_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoveryBatchRow(Base):
    __tablename__ = "discovery_batches"
    __table_args__ = (
        UniqueConstraint("edition_id", "request_hash", name="uq_discovery_batches_request"),
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_discovery_batches_tlp"),
        CheckConstraint(
            "char_length(request_hash) = 64 AND request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_discovery_batches_request_hash",
        ),
        CheckConstraint("status = 'completed'", name="ck_discovery_batches_status"),
        Index("ix_discovery_batches_edition", "edition_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    complementary_axis: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    discovery_model_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT"), nullable=False
    )
    structuring_model_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT"), nullable=False
    )
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(64), nullable=False)
    external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EditorialGroupRow(Base):
    __tablename__ = "editorial_groups"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({EDITORIAL_GROUP_STATUS_VALUES_SQL})",
            name="ck_editorial_groups_status",
        ),
        CheckConstraint(
            f"outcome IN ({GROUPING_OUTCOME_VALUES_SQL})",
            name="ck_editorial_groups_outcome",
        ),
        CheckConstraint(
            f"editorial_type IS NULL OR editorial_type IN ({EDITORIAL_TYPE_VALUES_SQL})",
            name="ck_editorial_groups_type",
        ),
        CheckConstraint(
            f"source_relationship_status IN ({RELATIONSHIP_STATUS_VALUES_SQL})",
            name="ck_editorial_groups_relationship",
        ),
        CheckConstraint(
            f"grouping_confidence IN ({GROUPING_CONFIDENCE_VALUES_SQL})",
            name="ck_editorial_groups_confidence",
        ),
        CheckConstraint("version > 0", name="ck_editorial_groups_version"),
        Index("ix_editorial_groups_edition", "edition_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_relationship_status: Mapped[str] = mapped_column(String(32), nullable=False)
    needs_source_verification: Mapped[bool] = mapped_column(Boolean, nullable=False)
    needs_source_expansion: Mapped[bool] = mapped_column(Boolean, nullable=False)
    grouping_confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    grouping_justification: Mapped[str] = mapped_column(Text, nullable=False)
    potential_historical_group_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT")
    )
    editorial_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HumanDecisionRow(Base):
    __tablename__ = "human_decisions"
    __table_args__ = (
        CheckConstraint(
            f"decision_type IN ({HUMAN_DECISION_VALUES_SQL})",
            name="ck_human_decisions_type",
        ),
        Index("ix_human_decisions_edition", "edition_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)
    group_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceCollectionRow(Base):
    __tablename__ = "source_collections"
    __table_args__ = (
        UniqueConstraint(
            "subject_id", "source_candidate_id", name="uq_source_collections_subject_candidate"
        ),
        CheckConstraint(
            f"state IN ({COLLECTION_STATE_VALUES_SQL})", name="ck_source_collections_state"
        ),
        CheckConstraint(
            f"proposed_role IN ({SOURCE_ROLE_VALUES_SQL})",
            name="ck_source_collections_role",
        ),
        CheckConstraint(
            f"relationship_status IN ({RELATIONSHIP_STATUS_VALUES_SQL})",
            name="ck_source_collections_relationship",
        ),
        CheckConstraint(
            "relationship_status <> 'verified' OR "
            "relationship_evidence LIKE 'human:%' OR "
            "relationship_evidence LIKE 'deterministic:%'",
            name="ck_source_collections_verified_evidence",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_source_collections_attempt_count"),
        Index("ix_source_collections_subject_state", "subject_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_batches.id", ondelete="RESTRICT"), nullable=False
    )
    source_candidate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_role: Mapped[str] = mapped_column(String(32), nullable=False)
    relationship_status: Mapped[str] = mapped_column(String(32), nullable=False)
    relationship_evidence: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    source_document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT")
    )
    decoded_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT")
    )
    latest_attempt_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("collection_attempts.id", ondelete="RESTRICT")
    )
    derived_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_artifacts.id", ondelete="RESTRICT")
    )
    fetch_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT")
    )
    fetch_policy_snapshot_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("collection_policy_snapshots.id", ondelete="RESTRICT")
    )
    fetch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_reason: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CollectionPolicySnapshotRow(Base):
    __tablename__ = "collection_policy_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_redirects: Mapped[int] = mapped_column(nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(nullable=False)
    max_download_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_expanded_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_decompression_ratio: Mapped[float] = mapped_column(nullable=False)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=False)
    allowed_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    blocked_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    collector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CollectionAttemptRow(Base):
    __tablename__ = "collection_attempts"
    __table_args__ = (
        CheckConstraint(
            f"outcome IN ({ATTEMPT_OUTCOME_VALUES_SQL})", name="ck_collection_attempts_outcome"
        ),
        CheckConstraint("size IS NULL OR size >= 0", name="ck_collection_attempts_size"),
        CheckConstraint(
            "sha256 IS NULL OR (char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_collection_attempts_sha256",
        ),
        CheckConstraint(
            "encoded_size IS NULL OR encoded_size >= 0",
            name="ck_collection_attempts_encoded_size",
        ),
        CheckConstraint(
            "decoded_size IS NULL OR decoded_size >= 0",
            name="ck_collection_attempts_decoded_size",
        ),
        Index("ix_collection_attempts_collection", "collection_id", "attempted_at"),
        Index("ix_collection_attempts_job", "job_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    collection_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_collections.id", ondelete="RESTRICT"), nullable=False
    )
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    configuration_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_snapshot_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("collection_policy_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    redirect_chain: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    http_status: Mapped[int | None] = mapped_column()
    declared_content_type: Mapped[str | None] = mapped_column(String(255))
    detected_content_type: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    encoded_size: Mapped[int | None] = mapped_column(BigInteger)
    encoded_sha256: Mapped[str | None] = mapped_column(String(64))
    decoded_size: Mapped[int | None] = mapped_column(BigInteger)
    decoded_sha256: Mapped[str | None] = mapped_column(String(64))
    content_encoding: Mapped[str | None] = mapped_column(String(64))
    allowed_headers: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)


class DerivedArtifactRow(Base):
    __tablename__ = "derived_artifacts"
    __table_args__ = (
        CheckConstraint("text_length >= 0", name="ck_derived_artifacts_text_length"),
        Index("ix_derived_artifacts_source", "source_document_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    text_blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    parser_name: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    text_length: Mapped[int] = mapped_column(BigInteger, nullable=False)
    publication_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClaimRow(Base):
    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(f"kind IN ({CLAIM_KIND_VALUES_SQL})", name="ck_claims_kind"),
        CheckConstraint("span_start >= 0 AND span_end > span_start", name="ck_claims_span"),
        Index("ix_claims_subject", "subject_id", "created_at"),
        Index("ix_claims_source", "source_document_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    derived_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    span_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    span_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(128), nullable=False)
    extraction_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    local_span_start: Mapped[int | None] = mapped_column(BigInteger)
    local_span_end: Mapped[int | None] = mapped_column(BigInteger)
    model_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RejectedModelProposalRow(Base):
    __tablename__ = "rejected_model_proposals"
    __table_args__ = (
        CheckConstraint(
            "char_length(proposal_hash) = 64 AND proposal_hash ~ '^[0-9a-f]{64}$'",
            name="ck_rejected_model_proposals_hash",
        ),
        Index("ix_rejected_model_proposals_source", "source_document_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    derived_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IndicatorRow(Base):
    __tablename__ = "indicators"
    __table_args__ = (
        CheckConstraint(f"kind IN ({INDICATOR_KIND_VALUES_SQL})", name="ck_indicators_kind"),
        CheckConstraint("span_start >= 0 AND span_end > span_start", name="ck_indicators_span"),
        Index("ix_indicators_subject", "subject_id", "created_at"),
        Index("ix_indicators_source", "source_document_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    derived_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("derived_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    original_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    span_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    span_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BriefEvidencePackRow(Base):
    __tablename__ = "brief_evidence_packs"
    __table_args__ = (
        UniqueConstraint("subject_id", "version", name="uq_brief_evidence_packs_version"),
        UniqueConstraint("subject_id", "content_hash", name="uq_brief_evidence_packs_content_hash"),
        CheckConstraint("version > 0", name="ck_brief_evidence_packs_version"),
        CheckConstraint(
            "char_length(content_hash) = 64 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_brief_evidence_packs_hash",
        ),
        Index("ix_brief_evidence_packs_subject", "subject_id", "version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_hashes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    indicators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    normalized_entities: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    uncertainties: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    human_decisions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BriefDraftRow(Base):
    __tablename__ = "brief_drafts"
    __table_args__ = (
        UniqueConstraint("subject_id", "version", name="uq_brief_drafts_version"),
        CheckConstraint("version > 0", name="ck_brief_drafts_version"),
        CheckConstraint(
            f"status IN ({BRIEF_DRAFT_STATUS_VALUES_SQL})", name="ck_brief_drafts_status"
        ),
        CheckConstraint(
            "char_length(pack_hash) = 64 AND pack_hash ~ '^[0-9a-f]{64}$'",
            name="ck_brief_drafts_pack_hash",
        ),
        Index("ix_brief_drafts_subject", "subject_id", "version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    pack_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("brief_evidence_packs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pack_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    limits: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    model_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_draft_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("brief_drafts.id", ondelete="RESTRICT")
    )
    regenerated_block_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubjectProductionRunRow(Base):
    __tablename__ = "subject_production_runs"
    __table_args__ = (
        UniqueConstraint("subject_id", "run_number", name="uq_subject_run_number"),
        CheckConstraint("version >= 1", name="ck_run_version"),
        CheckConstraint("run_number >= 1", name="ck_run_number"),
        CheckConstraint(f"status IN ({PRODUCTION_STATUS_VALUES_SQL})", name="ck_run_status"),
        CheckConstraint(f"current_stage IN ({PRODUCTION_STAGE_VALUES_SQL})", name="ck_run_stage"),
        CheckConstraint(f"profile IN ({PRODUCTION_PROFILE_VALUES_SQL})", name="ck_run_profile"),
        Index("ix_subject_production_runs_subject_id_created_at", "subject_id", "created_at"),
        Index("ix_subject_production_runs_edition_id_status", "edition_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_conversations.id", ondelete="SET NULL")
    )
    run_number: Mapped[int] = mapped_column(nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)


class ProductionArtifactRow(Base):
    __tablename__ = "production_artifacts"
    __table_args__ = (
        UniqueConstraint("production_run_id", "stage", "version", name="uq_run_stage_version"),
        CheckConstraint("version >= 1", name="ck_artifact_version"),
        CheckConstraint(
            f"stage IN ({PRODUCTION_ARTIFACT_STAGE_VALUES_SQL})", name="ck_artifact_stage"
        ),
        CheckConstraint(
            f"status IN ({PRODUCTION_ARTIFACT_STATUS_VALUES_SQL})", name="ck_artifact_status"
        ),
        CheckConstraint(
            "char_length(input_hash) = 64 AND input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_artifact_input_hash",
        ),
        Index("ix_production_artifacts_run_stage_version", "production_run_id", "stage", "version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("subject_production_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="SET NULL")
    )
    canonical_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT")
    )
    rendered_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="SET NULL")
    )
    model_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="SET NULL")
    )
    conversation_turn_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_conversation_turns.id", ondelete="SET NULL")
    )
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, name="metadata", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EditionProductionBatchRow(Base):
    __tablename__ = "edition_production_batches"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({PRODUCTION_BATCH_STATUS_VALUES_SQL})", name="ck_batch_status"
        ),
        CheckConstraint(f"profile IN ({PRODUCTION_PROFILE_VALUES_SQL})", name="ck_batch_profile"),
        Index("ix_edition_production_batches_edition_id_status", "edition_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(nullable=False)


class EditionProductionBatchItemRow(Base):
    __tablename__ = "edition_production_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "position", name="uq_batch_position"),
        UniqueConstraint("batch_id", "subject_id", name="uq_batch_subject"),
        CheckConstraint("position >= 1", name="ck_batch_item_position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("edition_production_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("subject_production_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

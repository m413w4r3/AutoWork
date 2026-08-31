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
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

MODEL_PROVIDER_VALUES_SQL = "'openai', 'qwen', 'fake'"
MODEL_ROLE_VALUES_SQL = "'research', 'structured_extraction', 'drafting', 'critic'"
MODEL_RUN_STATUS_VALUES_SQL = (
    "'running', 'waiting_background', 'needs_review', 'succeeded', 'failed', 'blocked'"
)
CONVERSATION_TRANSPORT_VALUES_SQL = "'chatgpt_bridge', 'openai_responses', 'application_managed'"
CONVERSATION_PURPOSE_VALUES_SQL = (
    "'discovery', 'analyst_assistance', 'pivot_research', 'drafting', 'critic', 'subject_research'"
)
CONVERSATION_STATUS_VALUES_SQL = (
    "'pending', 'ready', 'busy', 'needs_review', 'unavailable', 'archived'"
)
CONVERSATION_TURN_STATUS_VALUES_SQL = "'running', 'succeeded', 'failed', 'needs_review', 'blocked'"


class ModelRunRow(Base):
    __tablename__ = "model_runs"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({MODEL_PROVIDER_VALUES_SQL})", name="ck_model_runs_provider"
        ),
        CheckConstraint(f"model_role IN ({MODEL_ROLE_VALUES_SQL})", name="ck_model_runs_role"),
        CheckConstraint(f"status IN ({MODEL_RUN_STATUS_VALUES_SQL})", name="ck_model_runs_status"),
        CheckConstraint(
            "submission_state IN ('not_submitted', 'submitted_or_unknown')",
            name="ck_model_runs_submission_state",
        ),
        CheckConstraint("submission_attempt >= 0", name="ck_model_runs_submission_attempt"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_model_runs_duration"),
        CheckConstraint(
            "char_length(authorized_input_hash) = 64",
            name="ck_model_runs_input_hash_length",
        ),
        CheckConstraint(
            "char_length(evidence_pack_hash) = 64",
            name="ck_model_runs_evidence_hash_length",
        ),
        CheckConstraint(
            "jsonb_typeof(parameters) = 'object'", name="ck_model_runs_parameters_object"
        ),
        CheckConstraint(
            "jsonb_typeof(output_references) = 'array'",
            name="ck_model_runs_output_references_array",
        ),
        CheckConstraint(
            "(raw_output_chars IS NULL OR raw_output_chars >= 0) "
            "AND citation_count >= 0 AND extracted_url_count >= 0",
            name="ck_model_runs_output_diagnostic_counts",
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
    submission_state: Mapped[str] = mapped_column(String(32), nullable=False)
    submission_attempt: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
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
    __table_args__ = (
        CheckConstraint("value_sha256 ~ '^[0-9a-f]{64}$'", name="ck_model_output_rejections_hash"),
        Index("ix_model_output_rejections_run", "model_run_id", "created_at"),
    )

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
        CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (output_sha256 IS NULL OR output_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_model_conversation_turns_hashes",
        ),
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

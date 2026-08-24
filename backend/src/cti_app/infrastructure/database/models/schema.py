from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

JOB_STATUS_VALUES_SQL = "'queued', 'running', 'waiting_human', 'succeeded', 'failed', 'cancelled'"
BRIEF_DRAFT_STATUS_VALUES_SQL = "'draft', 'changes_requested', 'approved', 'promoted'"
PRODUCTION_PROFILE_VALUES_SQL = "'brief_auto', 'major_assisted'"
PRODUCTION_STATUS_VALUES_SQL = "'queued', 'running', 'ready', 'needs_review', 'failed', 'cancelled'"
PRODUCTION_STAGE_VALUES_SQL = "'sources', 'references', 'extraction', 'synthesis', 'assembly'"
PRODUCTION_ARTIFACT_STAGE_VALUES_SQL = "'references', 'extraction', 'synthesis', 'brief'"
PRODUCTION_ARTIFACT_STATUS_VALUES_SQL = "'verified', 'stale', 'needs_review'"
PRODUCTION_BATCH_STATUS_VALUES_SQL = (
    "'queued', 'running', 'completed', 'completed_with_issues', 'cancelled'"
)


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
    # Added by migration 0021; nullable because packs frozen before it have no
    # snapshot of origin.
    built_from_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "discovery_snapshots.id",
            name="fk_brief_evidence_packs_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    built_from_snapshot_version: Mapped[int | None] = mapped_column(nullable=True)
    covered_contribution_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="full")
    base_pack_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "brief_evidence_packs.id",
            name="fk_brief_evidence_packs_base",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )


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
    research_date: Mapped[date | None] = mapped_column(Date)
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

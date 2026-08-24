from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

PRODUCTION_PROFILE_VALUES_SQL = "'brief_auto', 'major_assisted'"
PRODUCTION_STATUS_VALUES_SQL = "'queued', 'running', 'ready', 'needs_review', 'failed', 'cancelled'"
PRODUCTION_STAGE_VALUES_SQL = "'sources', 'references', 'extraction', 'synthesis', 'assembly'"
PRODUCTION_ARTIFACT_STAGE_VALUES_SQL = "'references', 'extraction', 'synthesis', 'brief'"
PRODUCTION_ARTIFACT_STATUS_VALUES_SQL = "'verified', 'stale', 'needs_review'"
PRODUCTION_BATCH_STATUS_VALUES_SQL = (
    "'queued', 'running', 'completed', 'completed_with_issues', 'cancelled'"
)


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

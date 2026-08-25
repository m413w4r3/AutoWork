from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
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
from .classification import TLP_VALUES_SQL


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
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_discovery_payload_object"),
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
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(64), nullable=False)
    external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DiscoveryIntakeRow(Base):
    __tablename__ = "discovery_intakes"
    __table_args__ = (
        UniqueConstraint("edition_id", "sequence", name="uq_discovery_intakes_sequence"),
        UniqueConstraint("edition_id", "intake_hash", name="uq_discovery_intakes_hash"),
        UniqueConstraint("batch_id", name="uq_discovery_intakes_batch"),
        CheckConstraint("sequence > 0", name="ck_discovery_intakes_sequence"),
        Index("ix_discovery_intakes_edition", "edition_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(nullable=False)
    input_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    intake_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    research_model_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT"), nullable=False
    )
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    complementary_axis: Mapped[str] = mapped_column(String(500), nullable=False)
    batch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_batches.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DiscoveryMergeRunRow(Base):
    __tablename__ = "discovery_merge_runs"
    __table_args__ = (
        UniqueConstraint("merge_input_hash", name="uq_discovery_merge_runs_input_hash"),
        CheckConstraint("rebase_count BETWEEN 0 AND 2", name="ck_discovery_merge_runs_rebase"),
        Index("ix_discovery_merge_runs_edition", "edition_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    parent_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "discovery_snapshots.id",
            name="fk_discovery_merge_runs_parent_snapshot",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )
    intake_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_intakes.id", ondelete="RESTRICT"), nullable=False
    )
    planner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    merge_model_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_runs.id", ondelete="RESTRICT")
    )
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    blocking_version: Mapped[str] = mapped_column(String(64), nullable=False)
    merge_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    handle_map: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    included_subject_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    excluded_subject_count: Mapped[int] = mapped_column(nullable=False)
    raw_output_reference: Mapped[str | None] = mapped_column(Text)
    normalized_output_reference: Mapped[str | None] = mapped_column(Text)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    review_reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    plan_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    supersedes_merge_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_merge_runs.id", ondelete="RESTRICT")
    )
    rebase_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DiscoverySubjectIdentityRow(Base):
    __tablename__ = "discovery_subject_identities"
    __table_args__ = (
        UniqueConstraint("edition_id", "origin_key", name="uq_discovery_subject_origin"),
        CheckConstraint("status IN ('active', 'merged')", name="ck_discovery_subject_status"),
        CheckConstraint(
            "(status = 'active' AND merged_into_id IS NULL) OR "
            "(status = 'merged' AND merged_into_id IS NOT NULL)",
            name="ck_discovery_subject_merge_projection",
        ),
        Index("ix_discovery_subject_identities_edition", "edition_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    origin_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_merge_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_merge_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    merged_into_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_subject_identities.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DiscoverySnapshotRow(Base):
    __tablename__ = "discovery_snapshots"
    __table_args__ = (
        UniqueConstraint("edition_id", "version", name="uq_discovery_snapshots_version"),
        UniqueConstraint("intake_id", name="uq_discovery_snapshots_intake"),
        UniqueConstraint("merge_run_id", name="uq_discovery_snapshots_merge_run"),
        CheckConstraint("version > 0", name="ck_discovery_snapshots_version"),
        Index("ix_discovery_snapshots_edition", "edition_id", "version"),
        Index(
            "uq_discovery_snapshots_active_operational",
            "edition_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    parent_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_snapshots.id", ondelete="RESTRICT")
    )
    intake_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_intakes.id", ondelete="RESTRICT"), nullable=False
    )
    merge_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_merge_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    planner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subjects: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubjectMergeEventRow(Base):
    __tablename__ = "subject_merge_events"
    __table_args__ = (
        CheckConstraint(
            "from_subject_id <> into_subject_id", name="ck_subject_merge_events_distinct"
        ),
        Index("ix_subject_merge_events_edition", "edition_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    from_subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_subject_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    into_subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_subject_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    merge_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_merge_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubjectContributionRow(Base):
    __tablename__ = "subject_contributions"
    __table_args__ = (
        UniqueConstraint("intake_id", "candidate_key", name="uq_subject_contributions_candidate"),
        CheckConstraint("first_seen_version > 0", name="ck_subject_contributions_version"),
        Index("ix_subject_contributions_subject", "subject_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_subject_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    intake_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("discovery_intakes.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    first_seen_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    first_seen_version: Mapped[int] = mapped_column(nullable=False)
    contributed_title: Mapped[str] = mapped_column(String(1000), nullable=False)
    contributed_summary: Mapped[str] = mapped_column(Text, nullable=False)
    contributed_source_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    contributed_provisional_ioc_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    merge_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_merge_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    merge_group_index: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

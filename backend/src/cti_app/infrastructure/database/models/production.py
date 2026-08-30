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
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

PRODUCTION_STATUS_VALUES_SQL = "'queued', 'running', 'ready', 'needs_review', 'failed', 'cancelled'"
PRODUCTION_STAGE_VALUES_SQL = "'sources', 'references', 'extraction', 'synthesis', 'assembly'"
PRODUCTION_ARTIFACT_STAGE_VALUES_SQL = "'references', 'extraction', 'synthesis', 'publication'"
PRODUCTION_REUSE_STAGE_VALUES_SQL = "'references', 'extraction', 'synthesis'"
PRODUCTION_ARTIFACT_STATUS_VALUES_SQL = "'verified', 'stale', 'needs_review'"
PRODUCTION_BATCH_STATUS_VALUES_SQL = (
    "'queued', 'running', 'completed', 'completed_with_issues', 'cancelled'"
)
PRODUCTION_BATCH_PHASE_VALUES_SQL = "'initial', 'recovery', 'review'"
ANALYST_INVESTIGATION_STATUS_VALUES_SQL = (
    "'queued', 'running', 'awaiting_review', 'completed', 'exhausted', 'failed', 'cancelled'"
)
ANALYST_INVESTIGATION_STAGE_VALUES_SQL = (
    "'seeds', 'features', 'tooling', 'invariants', 'pivots', 'corpus', 'detection', 'note'"
)
ANALYST_DECISION_VALUES_SQL = (
    "'member_validate', 'member_reject', 'feature_validate', 'feature_reject', "
    "'pivot_approve', 'pivot_reject', 'note_approve', 'note_changes_requested'"
)
ANALYST_DECISION_TARGET_VALUES_SQL = (
    "'member', 'feature', 'tool', 'invariant', 'pivot', 'corpus', 'detection', 'note'"
)
SAMPLE_ACQUISITION_REASON_VALUES_SQL = "'seed', 'hit_review'"
SAMPLE_ACQUISITION_OUTCOME_VALUES_SQL = "'success', 'error'"
SAMPLE_ACQUISITION_HASH_FAMILY_VALUES_SQL = "'md5', 'sha1', 'sha256'"


class SubjectProductionRunRow(Base):
    __tablename__ = "subject_production_runs"
    __table_args__ = (
        UniqueConstraint("subject_id", "run_number", name="uq_subject_run_number"),
        CheckConstraint("version >= 1", name="ck_run_version"),
        CheckConstraint("run_number >= 1", name="ck_run_number"),
        CheckConstraint("pipeline_generation >= 0", name="ck_run_pipeline_generation"),
        CheckConstraint(
            "force_recompute_from_stage IS NULL OR force_recompute_from_stage IN "
            f"({PRODUCTION_REUSE_STAGE_VALUES_SQL})",
            name="ck_run_force_recompute_stage",
        ),
        CheckConstraint(f"status IN ({PRODUCTION_STATUS_VALUES_SQL})", name="ck_run_status"),
        CheckConstraint(f"current_stage IN ({PRODUCTION_STAGE_VALUES_SQL})", name="ck_run_stage"),
        Index("ix_subject_production_runs_subject_id_created_at", "subject_id", "created_at"),
        Index("ix_subject_production_runs_edition_id_status", "edition_id", "status"),
        Index(
            "uq_subject_production_one_active_run",
            "subject_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    references_conversation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_conversations.id", ondelete="SET NULL")
    )
    synthesis_conversation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_conversations.id", ondelete="SET NULL")
    )
    run_number: Mapped[int] = mapped_column(nullable=False)
    pipeline_generation: Mapped[int] = mapped_column(nullable=False, server_default="0")
    research_date: Mapped[date | None] = mapped_column(Date)
    force_recompute_from_stage: Mapped[str | None] = mapped_column(String(32))
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
        CheckConstraint(
            "reused_from_artifact_id IS NULL OR reused_from_artifact_id <> id",
            name="ck_artifact_reuse_not_self",
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
    reused_from_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("production_artifacts.id", ondelete="RESTRICT")
    )
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, name="metadata", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalystInvestigationRow(Base):
    __tablename__ = "analyst_investigations"
    __table_args__ = (
        UniqueConstraint("production_run_id", name="uq_analyst_investigation_run"),
        CheckConstraint("version >= 1", name="ck_analyst_investigation_version"),
        CheckConstraint("cycle_number >= 1", name="ck_analyst_investigation_cycle"),
        CheckConstraint("max_cycles >= 1", name="ck_analyst_investigation_max_cycles"),
        CheckConstraint(
            "max_pivot_runs >= 0 AND max_hits_acquired >= 0 AND max_new_samples >= 0 "
            "AND max_vt_read_units >= 0",
            name="ck_analyst_investigation_budget_max",
        ),
        CheckConstraint(
            "consumed_pivot_runs BETWEEN 0 AND max_pivot_runs AND "
            "consumed_hits_acquired BETWEEN 0 AND max_hits_acquired AND "
            "consumed_new_samples BETWEEN 0 AND max_new_samples AND "
            "consumed_vt_read_units BETWEEN 0 AND max_vt_read_units",
            name="ck_analyst_investigation_budget_consumed",
        ),
        CheckConstraint(
            f"status IN ({ANALYST_INVESTIGATION_STATUS_VALUES_SQL})",
            name="ck_analyst_investigation_status",
        ),
        CheckConstraint(
            f"current_stage IN ({ANALYST_INVESTIGATION_STAGE_VALUES_SQL})",
            name="ck_analyst_investigation_stage",
        ),
        CheckConstraint(
            "(input_pack_blob_id IS NULL) = (input_sha256 IS NULL)",
            name="ck_analyst_investigation_input_pair",
        ),
        CheckConstraint(
            "input_sha256 IS NULL OR (char_length(input_sha256) = 64 "
            "AND input_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_analyst_investigation_input_sha256",
        ),
        Index("ix_analyst_investigations_subject_status", "subject_id", "status"),
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
    synthesis_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    cycle_number: Mapped[int] = mapped_column(nullable=False)
    input_pack_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT")
    )
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    pivot_conversation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_conversations.id", ondelete="SET NULL")
    )
    max_cycles: Mapped[int] = mapped_column(nullable=False)
    max_pivot_runs: Mapped[int] = mapped_column(nullable=False)
    max_hits_acquired: Mapped[int] = mapped_column(nullable=False)
    max_new_samples: Mapped[int] = mapped_column(nullable=False)
    max_vt_read_units: Mapped[int] = mapped_column(nullable=False)
    consumed_pivot_runs: Mapped[int] = mapped_column(nullable=False)
    consumed_hits_acquired: Mapped[int] = mapped_column(nullable=False)
    consumed_new_samples: Mapped[int] = mapped_column(nullable=False)
    consumed_vt_read_units: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)


class AnalystDecisionRow(Base):
    __tablename__ = "analyst_decisions"
    __table_args__ = (
        CheckConstraint(
            f"decision_type IN ({ANALYST_DECISION_VALUES_SQL})", name="ck_analyst_decisions_type"
        ),
        CheckConstraint(
            f"target_type IN ({ANALYST_DECISION_TARGET_VALUES_SQL})",
            name="ck_analyst_decisions_target",
        ),
        Index("ix_analyst_decisions_investigation", "investigation_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalystInputPackRow(Base):
    __tablename__ = "analyst_input_packs"
    __table_args__ = (
        UniqueConstraint("investigation_id", name="uq_analyst_input_packs_investigation"),
        CheckConstraint(
            "char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_analyst_input_pack_sha256",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="CASCADE"),
        nullable=False,
    )
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SampleAcquisitionAttemptRow(Base):
    """Append-only ledger of VT byte-pull attempts, one row per attempt.

    A `SUCCESS` row is the canonical, DB-enforced replay marker for its
    `(investigation_id, requested_hash)` pair: at most one may exist per
    pair (partial unique index below), independent of any Python pre-check.
    """

    __tablename__ = "sample_acquisition_attempts"
    __table_args__ = (
        CheckConstraint(
            f"reason IN ({SAMPLE_ACQUISITION_REASON_VALUES_SQL})",
            name="ck_sample_acquisition_reason",
        ),
        CheckConstraint(
            f"outcome IN ({SAMPLE_ACQUISITION_OUTCOME_VALUES_SQL})",
            name="ck_sample_acquisition_outcome",
        ),
        CheckConstraint(
            f"hash_family IN ({SAMPLE_ACQUISITION_HASH_FAMILY_VALUES_SQL})",
            name="ck_sample_acquisition_hash_family",
        ),
        CheckConstraint(
            "requested_hash ~ '^[0-9a-f]{32}$' OR requested_hash ~ '^[0-9a-f]{40}$' "
            "OR requested_hash ~ '^[0-9a-f]{64}$'",
            name="ck_sample_acquisition_requested_hash",
        ),
        CheckConstraint(
            "(outcome = 'success' AND sample_id IS NOT NULL AND error_code IS NULL) OR "
            "(outcome = 'error' AND sample_id IS NULL AND error_code IS NOT NULL)",
            name="ck_sample_acquisition_outcome_shape",
        ),
        Index(
            "uq_sample_acquisition_success_replay",
            "investigation_id",
            "requested_hash",
            unique=True,
            postgresql_where=text("outcome = 'success'"),
        ),
        Index("ix_sample_acquisition_attempts_investigation", "investigation_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analyst_investigations.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_family: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    sample_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("samples.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EditionProductionBatchRow(Base):
    __tablename__ = "edition_production_batches"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({PRODUCTION_BATCH_STATUS_VALUES_SQL})", name="ck_batch_status"
        ),
        CheckConstraint(f"phase IN ({PRODUCTION_BATCH_PHASE_VALUES_SQL})", name="ck_batch_phase"),
        Index("ix_edition_production_batches_edition_id_status", "edition_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False, server_default="initial")
    next_dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        CheckConstraint(
            "auto_recovery_count BETWEEN 0 AND 1", name="ck_batch_item_auto_recovery_count"
        ),
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
    auto_recovery_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductionInputSnapshotRow(Base):
    __tablename__ = "production_input_snapshots"
    __table_args__ = (
        UniqueConstraint("production_run_id", name="uq_production_input_snapshots_run"),
        CheckConstraint("editorial_group_version >= 1", name="ck_production_input_group_version"),
        CheckConstraint("period_start <= period_end", name="ck_production_input_period_order"),
        CheckConstraint(
            "jsonb_typeof(core_sources) = 'array'", name="ck_production_input_sources_array"
        ),
        CheckConstraint(
            "char_length(input_hash) = 64 AND input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_production_input_hash",
        ),
        CheckConstraint(
            "char_length(reuse_basis_hash) = 64 AND reuse_basis_hash ~ '^[0-9a-f]{64}$'",
            name="ck_production_input_reuse_basis_hash",
        ),
        Index("ix_production_input_snapshots_subject", "subject_id", "captured_at"),
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
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    editorial_group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT"), nullable=False
    )
    editorial_group_version: Mapped[int] = mapped_column(nullable=False)
    subject_title: Mapped[str] = mapped_column(Text, nullable=False)
    subject_description: Mapped[str] = mapped_column(Text, nullable=False)
    actor_or_campaign: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    research_date: Mapped[date] = mapped_column(Date, nullable=False)
    core_sources: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reuse_basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductionReuseInvalidationRow(Base):
    __tablename__ = "production_reuse_invalidations"
    __table_args__ = (
        CheckConstraint(
            f"from_stage IN ({PRODUCTION_REUSE_STAGE_VALUES_SQL})",
            name="ck_production_reuse_invalidation_stage",
        ),
        Index(
            "ix_production_reuse_invalidations_subject",
            "edition_id",
            "subject_id",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    from_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

"""Relational records for editorial proposals and their immutable timeline."""

from __future__ import annotations

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
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

EDITORIAL_PROPOSAL_STATES_SQL = (
    "'preparing', 'proposed', 'refused', 'needs_review', 'accepted', 'rejected', 'stale'"
)
EDITORIAL_PROPOSAL_ORIGINS_SQL = "'bridge_draft', 'manual_patch'"
EDITORIAL_PROPOSAL_EVENT_KINDS_SQL = "'prepared', 'refused', 'needs_review', 'state_changed'"
EDITORIAL_PROPOSAL_OPERATIONS_SQL = (
    "'rewrite_title', 'rewrite_lead', 'rewrite_paragraph', 'insert_paragraph', "
    "'delete_paragraph', 'rewrite_section', 'insert_section', 'delete_section', "
    "'merge_sections', 'split_section', 'rename_section', 'create_table', "
    "'rewrite_table', 'delete_table', 'create_diagram', 'rewrite_diagram', "
    "'delete_diagram', 'create_chart', 'rewrite_chart', 'delete_chart', "
    "'create_source_figure', 'rewrite_source_figure', 'replace_source_figure', "
    "'delete_source_figure'"
)


class EditorialProposalRow(Base):
    __tablename__ = "editorial_proposals"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "actor_id",
            "idempotency_key",
            name="uq_editorial_proposals_subject_actor_idempotency",
        ),
        CheckConstraint(
            f"origin IN ({EDITORIAL_PROPOSAL_ORIGINS_SQL})",
            name="ck_editorial_proposals_origin",
        ),
        CheckConstraint(
            f"state IN ({EDITORIAL_PROPOSAL_STATES_SQL})",
            name="ck_editorial_proposals_state",
        ),
        CheckConstraint(
            f"operation IN ({EDITORIAL_PROPOSAL_OPERATIONS_SQL})",
            name="ck_editorial_proposals_operation",
        ),
        CheckConstraint(
            "base_artifact_version >= 1 AND state_version >= 1",
            name="ck_editorial_proposals_versions",
        ),
        CheckConstraint(
            "base_input_hash ~ '^[0-9a-f]{64}$' AND "
            "base_canonical_sha256 ~ '^[0-9a-f]{64}$' AND "
            "base_pin_hash ~ '^[0-9a-f]{64}$' AND "
            "before_hash ~ '^[0-9a-f]{64}$' AND "
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_proposals_hashes",
        ),
        CheckConstraint(
            "base_routing_hash IS NULL OR base_routing_hash ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_proposals_routing_hash",
        ),
        CheckConstraint(
            "patch_sha256 IS NULL OR patch_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_proposals_patch_sha256",
        ),
        CheckConstraint(
            "patch_blob_ref IS NULL AND patch_sha256 IS NULL OR "
            "patch_blob_ref IS NOT NULL AND patch_sha256 IS NOT NULL",
            name="ck_editorial_proposals_patch_reference",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_typeof(diagnostics) = 'object'",
            name="ck_editorial_proposals_json_shapes",
        ),
        CheckConstraint(
            "origin <> 'bridge_draft' OR thread_id IS NOT NULL",
            name="ck_editorial_proposals_bridge_thread",
        ),
        Index("ix_editorial_proposals_occurrence", "subject_id", "occurrence_id", "created_at"),
        Index("ix_editorial_proposals_variant", "variant_group_id", "state"),
        Index("ix_editorial_proposals_patch_blob", "patch_blob_ref"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("subjects.id", name="fk_editorial_proposals_subject", ondelete="RESTRICT"),
        nullable=False,
    )
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_runs.id", name="fk_editorial_proposals_run", ondelete="RESTRICT"),
        nullable=False,
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_workspace_snapshots.snapshot_id",
            name="fk_editorial_proposals_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    occurrence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    base_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_proposals_base_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    base_artifact_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    base_routing_hash: Mapped[str | None] = mapped_column(String(64))
    base_pin_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    before_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_blob_ref: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("blobs.id", name="fk_editorial_proposals_patch_blob", ondelete="RESTRICT"),
    )
    patch_sha256: Mapped[str | None] = mapped_column(String(64))
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    thread_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_threads.id", name="fk_editorial_proposals_thread", ondelete="RESTRICT"
        ),
    )
    variant_group_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    accepted_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_proposals_accepted_artifact",
            ondelete="RESTRICT",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EditorialProposalModelRunRow(Base):
    __tablename__ = "editorial_proposal_model_runs"
    __table_args__ = (
        UniqueConstraint("model_run_id", name="uq_editorial_proposal_model_runs_run"),
        UniqueConstraint("model_turn_id", name="uq_editorial_proposal_model_runs_turn"),
        CheckConstraint(
            "model_run_id IS NOT NULL AND model_turn_id IS NOT NULL",
            name="ck_editorial_proposal_model_runs_exact",
        ),
        Index("ix_editorial_proposal_model_runs_thread", "thread_id"),
    )

    proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_proposals.id",
            name="fk_editorial_proposal_model_runs_proposal",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    thread_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_threads.id",
            name="fk_editorial_proposal_model_runs_thread",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    model_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "model_runs.id", name="fk_editorial_proposal_model_runs_run", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    model_turn_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "model_conversation_turns.id",
            name="fk_editorial_proposal_model_runs_turn",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EditorialProposalEventRow(Base):
    __tablename__ = "editorial_proposal_events"
    __table_args__ = (
        UniqueConstraint("proposal_id", "sequence", name="uq_editorial_proposal_events_sequence"),
        UniqueConstraint(
            "proposal_id", "idempotency_key", name="uq_editorial_proposal_events_idempotency"
        ),
        CheckConstraint(
            "sequence >= 1 AND state_version >= 1", name="ck_editorial_proposal_events_counters"
        ),
        CheckConstraint(
            f"kind IN ({EDITORIAL_PROPOSAL_EVENT_KINDS_SQL})",
            name="ck_editorial_proposal_events_kind",
        ),
        CheckConstraint(
            f"state IN ({EDITORIAL_PROPOSAL_STATES_SQL})",
            name="ck_editorial_proposal_events_state",
        ),
        CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$' AND "
            "(patch_sha256 IS NULL OR patch_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_editorial_proposal_events_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(diagnostics) = 'object'", name="ck_editorial_proposal_events_diagnostics"
        ),
        Index("ix_editorial_proposal_events_timeline", "proposal_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_proposals.id",
            name="fk_editorial_proposal_events_proposal",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_sha256: Mapped[str | None] = mapped_column(String(64))
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

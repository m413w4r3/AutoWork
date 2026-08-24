from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
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

BRIEF_DRAFT_STATUS_VALUES_SQL = "'draft', 'changes_requested', 'approved', 'promoted'"


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

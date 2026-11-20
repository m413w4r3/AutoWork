from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EditorialDraftRow(Base):
    """Mutable, noncanonical draft metadata; text bytes remain in content storage."""

    __tablename__ = "editorial_drafts"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "occurrence_id",
            name="uq_editorial_drafts_subject_occurrence",
        ),
        CheckConstraint("base_artifact_version >= 1", name="ck_editorial_drafts_base_version"),
        CheckConstraint("version >= 1", name="ck_editorial_drafts_version"),
        CheckConstraint(
            "char_length(base_canonical_sha256) = 64 AND base_canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_drafts_base_sha256",
        ),
        CheckConstraint(
            "char_length(text_sha256) = 64 AND text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_drafts_text_sha256",
        ),
        CheckConstraint("expires_at > updated_at", name="ck_editorial_drafts_expiry"),
        Index("ix_editorial_drafts_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("subjects.id", name="fk_editorial_drafts_subject", ondelete="RESTRICT"),
        nullable=False,
    )
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_runs.id", name="fk_editorial_drafts_run", ondelete="RESTRICT"),
        nullable=False,
    )
    occurrence_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    base_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_workspace_snapshots.snapshot_id",
            name="fk_editorial_drafts_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    base_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id", name="fk_editorial_drafts_artifact", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    base_artifact_version: Mapped[int] = mapped_column(nullable=False)
    base_canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    text_blob_ref: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("blobs.id", name="fk_editorial_drafts_text_blob", ondelete="RESTRICT"),
        nullable=False,
    )
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

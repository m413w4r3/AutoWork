from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SourceMediaCandidateRow(Base):
    __tablename__ = "source_media_candidates"
    __table_args__ = (
        Index(
            "ix_source_media_candidates_subject_policy",
            "subject_id",
            "policy_sha256",
        ),
        Index("ix_source_media_candidates_blob_id", "blob_id"),
        CheckConstraint("char_length(policy_sha256) = 64", name="ck_source_media_policy_sha_len"),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'", name="ck_source_media_policy_sha_format"
        ),
        CheckConstraint(
            "sha256 IS NULL OR (char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_source_media_sha_format",
        ),
        CheckConstraint(
            "status IN ('CANDIDATE', 'EXCLUDED_BY_RULE', 'ACCEPTED_FOR_REVIEW', "
            "'COLLECTION_FAILED', 'PAGE_EXCERPT_NEEDED')",
            name="ck_source_media_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT")
    )
    sha256: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    page: Mapped[int | None] = mapped_column(Integer)
    page_bbox: Mapped[dict[str, float] | None] = mapped_column(JSONB)
    anchor: Mapped[str | None] = mapped_column(Text)
    dom_locator: Mapped[str | None] = mapped_column(Text)
    original_url: Mapped[str | None] = mapped_column(Text)
    requested_url: Mapped[str | None] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    alt_text: Mapped[str | None] = mapped_column(Text)
    caption_text: Mapped[str | None] = mapped_column(Text)
    nearby_heading_text: Mapped[str | None] = mapped_column(Text)
    collection_diagnostics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    perceptual_hash: Mapped[str | None] = mapped_column(String(32))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

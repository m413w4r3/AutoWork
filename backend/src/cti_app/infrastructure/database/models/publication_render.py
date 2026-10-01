from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from .base import Base


class PublicationRenderRow(Base):
    __tablename__ = "publication_renders"
    __table_args__ = (
        CheckConstraint("format IN ('pdf')", name="ck_publication_renders_format"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_publication_renders_status",
        ),
        CheckConstraint(
            "char_length(input_hash) = 64 AND input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_publication_renders_input_hash_format",
        ),
        CheckConstraint(
            "char_length(template_sha256) = 64 AND template_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_publication_renders_template_sha256_format",
        ),
        CheckConstraint(
            "output_sha256 IS NULL OR (char_length(output_sha256) = 64 "
            "AND output_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_publication_renders_output_sha256_format",
        ),
        CheckConstraint(
            "char_length(btrim(font_bundle_version)) > 0",
            name="ck_publication_renders_font_bundle_version",
        ),
        CheckConstraint(
            "char_length(btrim(render_policy_version)) > 0",
            name="ck_publication_renders_render_policy_version",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR (output_blob_id IS NOT NULL "
            "AND output_sha256 IS NOT NULL AND output_byte_size IS NOT NULL "
            "AND output_byte_size > 0)",
            name="ck_publication_renders_succeeded_output",
        ),
        CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL",
            name="ck_publication_renders_failed_error_code",
        ),
        UniqueConstraint("input_hash", name="uq_publication_renders_input_hash"),
        Index(
            "ix_publication_renders_artifact_created_at",
            "publication_artifact_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    publication_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    renderer: Mapped[str] = mapped_column(String, nullable=False)
    renderer_version: Mapped[str] = mapped_column(String, nullable=False)
    template_version: Mapped[str] = mapped_column(String, nullable=False)
    template_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    compiler: Mapped[str] = mapped_column(String, nullable=False)
    compiler_version: Mapped[str] = mapped_column(String, nullable=False)
    font_bundle_version: Mapped[str] = mapped_column(String, nullable=False)
    render_policy_version: Mapped[str] = mapped_column(String, nullable=False)
    format: Mapped[str] = mapped_column(String, nullable=False)
    input_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=True
    )
    render_data_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=True
    )
    output_blob_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=True
    )
    output_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    output_byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

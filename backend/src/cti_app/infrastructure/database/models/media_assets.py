from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from .base import Base


class MediaAssetRow(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint("sha256", "mime_type", name="uq_media_assets_sha256_mime_type"),
        CheckConstraint("kind IN ('diagram_svg', 'source_figure')", name="ck_media_assets_kind"),
        CheckConstraint("byte_size > 0", name="ck_media_assets_byte_size_positive"),
        CheckConstraint("length(btrim(mime_type)) > 0", name="ck_media_assets_mime_type_nonempty"),
        CheckConstraint("length(btrim(source)) > 0", name="ck_media_assets_source_nonempty"),
        CheckConstraint(
            "length(btrim(policy_version)) > 0",
            name="ck_media_assets_policy_version_nonempty",
        ),
        CheckConstraint(
            "char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_media_assets_sha256_format",
        ),
        CheckConstraint(
            "((kind = 'diagram_svg' AND mime_type = 'image/svg+xml' "
            "AND compiler_name IS NOT NULL AND compiler_version IS NOT NULL "
            "AND provenance IS NULL AND locator IS NULL AND decision IS NULL) OR "
            "(kind = 'source_figure' AND provenance IS NOT NULL AND locator IS NOT NULL "
            "AND decision IS NOT NULL AND compiler_name IS NULL AND compiler_version IS NULL))",
            name="ck_media_assets_kind_metadata",
        ),
    )

    asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    compiler_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    compiler_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    provenance: Mapped[str | None] = mapped_column(Text, nullable=True)
    locator: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

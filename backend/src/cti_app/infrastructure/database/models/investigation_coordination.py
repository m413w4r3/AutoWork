"""PostgreSQL rows for durable investigation plan versions (M24-04A1).

``investigation_plan_versions`` is the sole authority for plan content; the
``investigation_plans`` row is the compare-and-swap head.  Versions are
append-only by PostgreSQL triggers (migration 0020), carry the canonical JSON
hash, and may only name a persisted advisor response of the same investigation
as their source.  ``supersedes_id`` is a composite FK so a version can only
supersede another version of its own investigation.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

INVESTIGATION_PLAN_SCHEMA_VERSION_CHECK_SQL = "schema_version = 'investigation-plan.v1'"
INVESTIGATION_PLAN_PARSE_STATUS_VALUES_SQL = "'parsed', 'partially_parsed'"
_INVESTIGATION_PLAN_CONTENT_IDENTITY_SQL = (
    "content ? 'investigation_id' AND content ? 'version' "
    "AND content ? 'schema_version' AND content ? 'leads' "
    "AND content->>'investigation_id' = investigation_id::text "
    "AND content->>'version' = version::text "
    "AND content->>'schema_version' = schema_version"
)


class InvestigationPlanRow(Base):
    """The current version of one investigation's plan (CAS head)."""

    __tablename__ = "investigation_plans"
    __table_args__ = (
        CheckConstraint("current_version >= 1", name="ck_investigation_plans_current_version"),
    )

    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "analyst_investigations.id",
            name="fk_investigation_plans_investigation",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    current_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationPlanVersionRow(Base):
    """One immutable, hashed plan version and its verified consultation source."""

    __tablename__ = "investigation_plan_versions"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "version",
            name="uq_investigation_plan_versions_investigation_version",
        ),
        UniqueConstraint(
            "source_invocation_id",
            "source_observation_id",
            name="uq_investigation_plan_versions_source",
        ),
        UniqueConstraint("id", "investigation_id", name="uq_investigation_plan_versions_identity"),
        ForeignKeyConstraint(
            ["supersedes_id", "investigation_id"],
            [
                "investigation_plan_versions.id",
                "investigation_plan_versions.investigation_id",
            ],
            name="fk_investigation_plan_versions_supersedes",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version >= 1", name="ck_investigation_plan_versions_version"),
        CheckConstraint(
            INVESTIGATION_PLAN_SCHEMA_VERSION_CHECK_SQL,
            name="ck_investigation_plan_versions_schema_version",
        ),
        CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_investigation_plan_versions_content_object",
        ),
        CheckConstraint(
            "char_length(content_sha256) = 64 AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_plan_versions_content_sha256",
        ),
        CheckConstraint(
            f"parse_status IN ({INVESTIGATION_PLAN_PARSE_STATUS_VALUES_SQL})",
            name="ck_investigation_plan_versions_parse_status",
        ),
        CheckConstraint(
            _INVESTIGATION_PLAN_CONTENT_IDENTITY_SQL,
            name="ck_investigation_plan_versions_content_identity",
        ),
        CheckConstraint(
            "jsonb_typeof(content->'leads') = 'array'",
            name="ck_investigation_plan_versions_leads_array",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "analyst_investigations.id",
            name="fk_investigation_plan_versions_investigation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_invocation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "tool_invocations.id",
            name="fk_investigation_plan_versions_source_invocation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    source_observation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "evidence_observations.id",
            name="fk_investigation_plan_versions_source_observation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

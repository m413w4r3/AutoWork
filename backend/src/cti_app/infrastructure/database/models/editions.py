from datetime import date, datetime
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .classification import TLP_VALUES_SQL

EDITION_STATUS_VALUES_SQL = (
    "'draft', 'discovery', 'selection', 'production', 'review', "
    "'assembling', 'published', 'archived'"
)


class EditionRow(Base):
    __tablename__ = "editions"
    __table_args__ = (
        UniqueConstraint(
            "country_code",
            "period_start",
            "period_end",
            name="uq_editions_country_period",
        ),
        CheckConstraint(f"tlp IN ({TLP_VALUES_SQL})", name="ck_editions_tlp"),
        CheckConstraint(f"status IN ({EDITION_STATUS_VALUES_SQL})", name="ck_editions_status"),
        CheckConstraint("version >= 1", name="ck_editions_version"),
        CheckConstraint("target_major_articles BETWEEN 0 AND 20", name="ck_editions_major"),
        CheckConstraint("target_briefs BETWEEN 0 AND 100", name="ck_editions_briefs"),
        CheckConstraint("period_start <= period_end", name="ck_editions_period_order"),
        CheckConstraint(
            "period_start = date_trunc('month', period_start)::date "
            "AND period_end = (date_trunc('month', period_start) + "
            "interval '1 month - 1 day')::date",
            name="ck_editions_complete_month",
        ),
        CheckConstraint("jsonb_typeof(languages) = 'array'", name="ck_editions_languages"),
        Index("ix_editions_country_status", "country_code", "status"),
        Index("ix_editions_period", "period_start", "period_end"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    country: Mapped[str] = mapped_column(String(100), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    period_start: Mapped[date] = mapped_column(nullable=False)
    period_end: Mapped[date] = mapped_column(nullable=False)
    tlp: Mapped[str] = mapped_column(String(16), nullable=False)
    languages: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    target_major_articles: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_briefs: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_edition_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="SET NULL")
    )
    source_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EditionAuditEventRow(Base):
    __tablename__ = "edition_audit_events"
    __table_args__ = (Index("ix_edition_audit_edition", "edition_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

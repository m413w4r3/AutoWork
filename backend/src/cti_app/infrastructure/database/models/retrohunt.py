"""Durable slots enforcing the two Retrohunt submissions per subject cap."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RetrohuntReservationRow(Base):
    __tablename__ = "retrohunt_reservations"
    __table_args__ = (
        UniqueConstraint("subject_id", "slot_number", name="uq_retrohunt_subject_slot"),
        UniqueConstraint("subject_id", "operation_key", name="uq_retrohunt_subject_operation"),
        CheckConstraint("slot_number IN (1, 2)", name="ck_retrohunt_slot_number"),
        CheckConstraint(
            "state IN ('reserved','submitting','submitted','completed','failed',"
            "'reconciliation_required')",
            name="ck_retrohunt_state",
        ),
        CheckConstraint("operation_key ~ '^[0-9a-f]{64}$'", name="ck_retrohunt_key_sha256"),
        CheckConstraint("rule_source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_retrohunt_rule_sha256"),
        CheckConstraint("corpus_sha256 ~ '^[0-9a-f]{64}$'", name="ck_retrohunt_corpus_sha256"),
        CheckConstraint("version >= 1", name="ck_retrohunt_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    slot_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rule_source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_id: Mapped[str] = mapped_column(String(256), nullable=False)
    corpus_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    submission_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    provider_job_id: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

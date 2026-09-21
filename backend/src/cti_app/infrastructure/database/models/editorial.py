from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

HUMAN_DECISION_VALUES_SQL = (
    "'claim_validate', 'claim_correct', "
    "'claim_reject', 'indicator_validate', 'indicator_correct', 'indicator_reject', "
    "'source_relationship_validate', 'source_relationship_correct'"
)


class HumanDecisionRow(Base):
    __tablename__ = "human_decisions"
    __table_args__ = (
        CheckConstraint(
            f"decision_type IN ({HUMAN_DECISION_VALUES_SQL})",
            name="ck_human_decisions_type",
        ),
        CheckConstraint(
            "jsonb_typeof(subject_ids) = 'array'", name="ck_human_decisions_subjects_array"
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_human_decisions_payload_object"
        ),
        Index("ix_human_decisions_edition", "edition_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

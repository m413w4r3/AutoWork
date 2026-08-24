from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .source_relationships import RELATIONSHIP_STATUS_VALUES_SQL

EDITORIAL_GROUP_STATUS_VALUES_SQL = "'proposed', 'rejected', 'selected', 'superseded'"
GROUPING_OUTCOME_VALUES_SQL = (
    "'new_subject', 'duplicate_same_publication', 'update_previous_subject', "
    "'non_independent_reprint', 'ambiguous_review'"
)
EDITORIAL_TYPE_VALUES_SQL = "'brief', 'major'"
GROUPING_CONFIDENCE_VALUES_SQL = "'low', 'medium', 'high'"
HUMAN_DECISION_VALUES_SQL = (
    "'merge', 'split', 'reject', 'select', 'claim_validate', 'claim_correct', "
    "'claim_reject', 'indicator_validate', 'indicator_correct', 'indicator_reject', "
    "'source_relationship_validate', 'source_relationship_correct', "
    "'brief_changes_requested', 'brief_approve', 'brief_promote'"
)


class EditorialGroupRow(Base):
    __tablename__ = "editorial_groups"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({EDITORIAL_GROUP_STATUS_VALUES_SQL})",
            name="ck_editorial_groups_status",
        ),
        CheckConstraint(
            f"outcome IN ({GROUPING_OUTCOME_VALUES_SQL})",
            name="ck_editorial_groups_outcome",
        ),
        CheckConstraint(
            f"editorial_type IS NULL OR editorial_type IN ({EDITORIAL_TYPE_VALUES_SQL})",
            name="ck_editorial_groups_type",
        ),
        CheckConstraint(
            f"source_relationship_status IN ({RELATIONSHIP_STATUS_VALUES_SQL})",
            name="ck_editorial_groups_relationship",
        ),
        CheckConstraint(
            f"grouping_confidence IN ({GROUPING_CONFIDENCE_VALUES_SQL})",
            name="ck_editorial_groups_confidence",
        ),
        CheckConstraint("version > 0", name="ck_editorial_groups_version"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_editorial_payload_object"),
        Index("ix_editorial_groups_edition", "edition_id", "status", "created_at"),
        Index("ix_editorial_groups_discovery_subject", "discovery_subject_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_relationship_status: Mapped[str] = mapped_column(String(32), nullable=False)
    needs_source_verification: Mapped[bool] = mapped_column(Boolean, nullable=False)
    needs_source_expansion: Mapped[bool] = mapped_column(Boolean, nullable=False)
    grouping_confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    grouping_justification: Mapped[str] = mapped_column(Text, nullable=False)
    potential_historical_group_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editorial_groups.id", ondelete="RESTRICT")
    )
    editorial_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    discovery_subject_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_subject_identities.id", ondelete="RESTRICT"),
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HumanDecisionRow(Base):
    __tablename__ = "human_decisions"
    __table_args__ = (
        CheckConstraint(
            f"decision_type IN ({HUMAN_DECISION_VALUES_SQL})",
            name="ck_human_decisions_type",
        ),
        CheckConstraint(
            "jsonb_typeof(group_ids) = 'array'", name="ck_human_decisions_groups_array"
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
    group_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

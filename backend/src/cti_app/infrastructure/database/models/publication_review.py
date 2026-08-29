"""SQLAlchemy rows for append-only publication review decisions."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PublicationReviewDecisionRow(Base):
    __tablename__ = "publication_review_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('include', 'exclude')",
            name="ck_publication_review_decision",
        ),
        CheckConstraint(
            "pipeline_generation >= 0",
            name="ck_publication_review_generation",
        ),
        CheckConstraint(
            "document_artifact_version >= 1",
            name="ck_publication_review_artifact_version",
        ),
        CheckConstraint(
            "char_length(document_input_hash) = 64 "
            "AND document_input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_publication_review_input_hash",
        ),
        CheckConstraint(
            "char_length(btrim(actor_id)) > 0",
            name="ck_publication_review_actor",
        ),
        CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 500",
            name="ck_publication_review_reason_length",
        ),
        CheckConstraint(
            "decision <> 'exclude' OR char_length(btrim(reason)) > 0",
            name="ck_publication_review_exclude_reason",
        ),
        Index(
            "ix_publication_review_edition_occurred",
            "edition_id",
            "occurred_at",
            "id",
        ),
        Index(
            "ix_publication_review_subject_history",
            "subject_id",
            "occurred_at",
            "id",
        ),
        Index(
            "ix_publication_review_current_artifact",
            "production_run_id",
            "pipeline_generation",
            "document_artifact_id",
            "document_artifact_version",
            "document_input_hash",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("subject_production_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pipeline_generation: Mapped[int] = mapped_column(nullable=False)
    document_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_artifact_version: Mapped[int] = mapped_column(nullable=False)
    document_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SelectionDecisionRow(Base):
    __tablename__ = "selection_decisions"
    __table_args__ = (
        UniqueConstraint(
            "edition_id",
            "discovery_subject_id",
            "idempotency_key",
            name="uq_selection_decisions_idempotency",
        ),
        CheckConstraint(
            "action IN ('select', 'ignore')",
            name="ck_selection_decisions_action",
        ),
        CheckConstraint(
            "(action = 'select' AND subject_id IS NOT NULL) OR "
            "(action = 'ignore' AND subject_id IS NULL)",
            name="ck_selection_decisions_subject_action",
        ),
        CheckConstraint("snapshot_version > 0", name="ck_selection_decisions_snapshot_version"),
        CheckConstraint("char_length(btrim(actor_id)) > 0", name="ck_selection_decisions_actor"),
        CheckConstraint(
            "char_length(btrim(correlation_id)) > 0",
            name="ck_selection_decisions_correlation",
        ),
        CheckConstraint(
            "char_length(btrim(idempotency_key)) > 0",
            name="ck_selection_decisions_idempotency",
        ),
        Index(
            "ix_selection_decisions_edition_subject_occurred",
            "edition_id",
            "discovery_subject_id",
            "occurred_at",
        ),
        Index("ix_selection_decisions_edition_occurred", "edition_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    discovery_subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_subject_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    snapshot_version: Mapped[int] = mapped_column(nullable=False)
    subject_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubjectDiscoveryOriginRow(Base):
    __tablename__ = "subject_discovery_origins"
    __table_args__ = (
        UniqueConstraint(
            "edition_id",
            "discovery_subject_id",
            name="uq_subject_discovery_origins_identity",
        ),
        UniqueConstraint("selection_decision_id", name="uq_subject_discovery_origins_decision"),
        CheckConstraint(
            "selected_snapshot_version > 0",
            name="ck_subject_discovery_origins_snapshot_version",
        ),
        Index(
            "ix_subject_discovery_origins_edition_discovery",
            "edition_id",
            "discovery_subject_id",
        ),
    )

    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), primary_key=True
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    discovery_subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_subject_identities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    selection_decision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("selection_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    selected_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("discovery_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    selected_snapshot_version: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

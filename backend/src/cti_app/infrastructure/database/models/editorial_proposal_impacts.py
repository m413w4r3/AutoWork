"""Transactional outbox for post-adoption editorial impacts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

EDITORIAL_PROPOSAL_IMPACT_STATES_SQL = "'pending', 'running', 'done', 'failed', 'superseded'"


class EditorialProposalImpactRow(Base):
    __tablename__ = "editorial_proposal_impacts"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_editorial_proposal_impacts_proposal"),
        UniqueConstraint(
            "production_run_id",
            "sequence",
            name="uq_editorial_proposal_impacts_run_sequence",
        ),
        CheckConstraint("sequence >= 1", name="ck_editorial_proposal_impacts_sequence"),
        CheckConstraint(
            f"state IN ({EDITORIAL_PROPOSAL_IMPACT_STATES_SQL})",
            name="ck_editorial_proposal_impacts_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_editorial_proposal_impacts_attempts"),
        CheckConstraint(
            "base_pin_hash ~ '^[0-9a-f]{64}$' AND accepted_pin_hash ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_proposal_impacts_pin_hashes",
        ),
        CheckConstraint(
            "state IN ('pending', 'running') OR completed_at IS NOT NULL",
            name="ck_editorial_proposal_impacts_completion",
        ),
        Index("ix_editorial_proposal_impacts_dispatch", "state", "available_at", "sequence"),
    )

    impact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "subjects.id", name="fk_editorial_proposal_impacts_subject", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_runs.id", name="fk_editorial_proposal_impacts_run", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_proposals.id",
            name="fk_editorial_proposal_impacts_proposal",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    accepted_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_proposal_impacts_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    accepted_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_workspace_snapshots.snapshot_id",
            name="fk_editorial_proposal_impacts_snapshot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    base_pin_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_pin_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

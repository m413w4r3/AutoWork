from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, SmallInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class BridgeGenerationSlotRow(Base):
    """The one durable capacity lease for editorial discussion generations."""

    __tablename__ = "bridge_generation_slots"
    __table_args__ = (
        CheckConstraint("singleton_key = 1", name="ck_bridge_generation_slots_singleton"),
        CheckConstraint("lease_token >= 0", name="ck_bridge_generation_slots_token"),
        CheckConstraint(
            "state IN ('free', 'held', 'held_unknown')",
            name="ck_bridge_generation_slots_state",
        ),
        CheckConstraint(
            "(state = 'free' AND holder_thread_id IS NULL AND holder_job_id IS NULL "
            "AND holder_turn_id IS NULL AND acquired_at IS NULL AND lease_expires_at IS NULL) "
            "OR (state IN ('held', 'held_unknown') AND holder_thread_id IS NOT NULL "
            "AND holder_job_id IS NOT NULL AND acquired_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_bridge_generation_slots_holder_state",
        ),
    )

    singleton_key: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    holder_thread_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "editorial_threads.id",
            name="fk_bridge_generation_slots_thread",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    holder_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "editorial_thread_jobs.id",
            name="fk_bridge_generation_slots_job",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    holder_turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "model_conversation_turns.id",
            name="fk_bridge_generation_slots_turn",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    lease_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'free'"))

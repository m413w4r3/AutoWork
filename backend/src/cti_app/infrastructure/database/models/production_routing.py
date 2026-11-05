"""Immutable subject publication routing decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cti_app.infrastructure.database.models.base import Base


class ProductionRoutingDecisionRow(Base):
    __tablename__ = "production_routing_decisions"
    __table_args__ = (
        UniqueConstraint(
            "production_run_id",
            "pipeline_generation",
            "input_hash",
            name="uq_production_routing_run_generation_input",
        ),
        CheckConstraint("pipeline_generation >= 0", name="ck_routing_generation"),
        CheckConstraint("status IN ('decided', 'needs_review')", name="ck_routing_status"),
        CheckConstraint(
            "publication_kind IS NULL OR publication_kind IN ('article', 'brief')",
            name="ck_routing_publication_kind",
        ),
        CheckConstraint(
            "(status = 'decided' AND publication_kind IS NOT NULL) OR "
            "(status = 'needs_review' AND publication_kind IS NULL)",
            name="ck_routing_status_kind",
        ),
        CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$' AND decision_hash ~ '^[0-9a-f]{64}$'",
            name="ck_routing_hashes",
        ),
        CheckConstraint(
            "extraction_artifact_hash IS NULL OR extraction_artifact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_routing_extraction_hash",
        ),
        CheckConstraint(
            "relevance_artifact_hash IS NULL OR relevance_artifact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_routing_relevance_hash",
        ),
        CheckConstraint(
            "confirmed_ioc_count >= 0 AND contextual_ioc_count >= 0 AND ambiguous_ioc_count >= 0",
            name="ck_routing_ioc_counts",
        ),
        CheckConstraint(
            "jsonb_typeof(critical_source_diagnostics) = 'array'",
            name="ck_routing_diagnostics_array",
        ),
        CheckConstraint("char_length(policy_version) > 0", name="ck_routing_policy_version"),
        Index(
            "ix_production_routing_decisions_run_created",
            "production_run_id",
            "decided_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_runs.id",
            name="fk_production_routing_decisions_run",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    pipeline_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_production_routing_decisions_extraction_artifact",
            ondelete="RESTRICT",
        ),
    )
    extraction_artifact_hash: Mapped[str | None] = mapped_column(String(64))
    relevance_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_production_routing_decisions_relevance_artifact",
            ondelete="RESTRICT",
        ),
    )
    relevance_artifact_hash: Mapped[str | None] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    publication_kind: Mapped[str | None] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_ioc_count: Mapped[int] = mapped_column(Integer, nullable=False)
    contextual_ioc_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ambiguous_ioc_count: Mapped[int] = mapped_column(Integer, nullable=False)
    critical_source_diagnostics: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)

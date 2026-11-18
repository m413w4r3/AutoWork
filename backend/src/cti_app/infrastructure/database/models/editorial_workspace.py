from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EditorialWorkspaceSnapshotRow(Base):
    """Immutable pin and occurrence registry for one workspace snapshot."""

    __tablename__ = "editorial_workspace_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "production_run_id",
            "workspace_pin_hash",
            name="uq_editorial_workspace_snapshots_run_pin",
        ),
        CheckConstraint(
            "synthesis_version >= 1 AND char_length(synthesis_input_hash) = 64 AND "
            "synthesis_input_hash ~ '^[0-9a-f]{64}$' AND "
            "char_length(synthesis_canonical_sha256) = 64 AND "
            "synthesis_canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_workspace_snapshots_synthesis_hashes",
        ),
        CheckConstraint(
            "(enrichment_artifact_id IS NULL AND enrichment_version IS NULL AND "
            "enrichment_input_hash IS NULL AND enrichment_canonical_sha256 IS NULL) OR "
            "(enrichment_artifact_id IS NOT NULL AND enrichment_version >= 1 AND "
            "enrichment_input_hash IS NOT NULL AND char_length(enrichment_input_hash) = 64 AND "
            "enrichment_input_hash ~ '^[0-9a-f]{64}$' AND "
            "enrichment_canonical_sha256 IS NOT NULL AND "
            "char_length(enrichment_canonical_sha256) = 64 AND "
            "enrichment_canonical_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_editorial_workspace_snapshots_enrichment_pin",
        ),
        CheckConstraint(
            "(publication_artifact_id IS NULL AND publication_version IS NULL AND "
            "publication_input_hash IS NULL AND publication_canonical_sha256 IS NULL) OR "
            "(publication_artifact_id IS NOT NULL AND publication_version >= 1 AND "
            "publication_input_hash IS NOT NULL AND char_length(publication_input_hash) = 64 AND "
            "publication_input_hash ~ '^[0-9a-f]{64}$' AND "
            "publication_canonical_sha256 IS NOT NULL AND "
            "char_length(publication_canonical_sha256) = 64 AND "
            "publication_canonical_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_editorial_workspace_snapshots_publication_pin",
        ),
        CheckConstraint(
            "routing_decision_hash IS NULL OR "
            "(char_length(routing_decision_hash) = 64 AND "
            "routing_decision_hash ~ '^[0-9a-f]{64}$')",
            name="ck_editorial_workspace_snapshots_routing_pin",
        ),
        CheckConstraint(
            "publication_kind IS NULL OR publication_kind IN ('article', 'brief')",
            name="ck_editorial_workspace_snapshots_publication_kind",
        ),
        CheckConstraint(
            "char_length(workspace_pin_hash) = 64 AND workspace_pin_hash ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_workspace_snapshots_pin_hash",
        ),
        CheckConstraint(
            "char_length(registry_hash) = 64 AND registry_hash ~ '^[0-9a-f]{64}$'",
            name="ck_editorial_workspace_snapshots_registry_hash",
        ),
        CheckConstraint(
            "pipeline_generation >= 0",
            name="ck_editorial_workspace_snapshots_pipeline_generation",
        ),
        CheckConstraint(
            "jsonb_typeof(block_registry) = 'array'",
            name="ck_editorial_workspace_snapshots_registry_array",
        ),
        CheckConstraint(
            "jsonb_typeof(retired_block_ids) = 'array'",
            name="ck_editorial_workspace_snapshots_retired_array",
        ),
        Index("ix_editorial_workspace_snapshots_subject", "subject_id", "created_at"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "subjects.id", name="fk_editorial_workspace_snapshots_subject", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_runs.id", name="fk_editorial_workspace_snapshots_run", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    synthesis_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_workspace_snapshots_synthesis_artifact",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    synthesis_version: Mapped[int] = mapped_column(nullable=False)
    synthesis_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    synthesis_canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    enrichment_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_workspace_snapshots_enrichment_artifact",
            ondelete="RESTRICT",
        ),
    )
    enrichment_version: Mapped[int | None] = mapped_column()
    enrichment_input_hash: Mapped[str | None] = mapped_column(String(64))
    enrichment_canonical_sha256: Mapped[str | None] = mapped_column(String(64))
    publication_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_artifacts.id",
            name="fk_editorial_workspace_snapshots_publication_artifact",
            ondelete="RESTRICT",
        ),
    )
    publication_version: Mapped[int | None] = mapped_column()
    publication_input_hash: Mapped[str | None] = mapped_column(String(64))
    publication_canonical_sha256: Mapped[str | None] = mapped_column(String(64))
    pipeline_generation: Mapped[int] = mapped_column(nullable=False)
    routing_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "production_routing_decisions.id",
            name="fk_editorial_workspace_snapshots_routing_decision",
            ondelete="RESTRICT",
        ),
    )
    routing_decision_hash: Mapped[str | None] = mapped_column(String(64))
    publication_kind: Mapped[str | None] = mapped_column(String(16))
    workspace_pin_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    block_registry: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    retired_block_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    parent_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "editorial_workspace_snapshots.snapshot_id",
            name="fk_editorial_workspace_snapshots_parent",
            ondelete="RESTRICT",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

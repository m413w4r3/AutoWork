"""ORM rows for the canonical, versioned edition composition."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EditionCompositionRow(Base):
    __tablename__ = "edition_compositions"
    __table_args__ = (
        UniqueConstraint("edition_id", name="uq_edition_compositions_edition"),
        CheckConstraint("version >= 1", name="ck_edition_compositions_version"),
        CheckConstraint(
            "preparation_status IN ('unassessed', 'ready', 'blocked')",
            name="ck_edition_compositions_preparation_status",
        ),
        CheckConstraint("char_length(btrim(actor_id)) > 0", name="ck_edition_compositions_actor"),
        Index("ix_edition_compositions_edition_version", "edition_id", "version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    preparation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EditionCompositionEntryRow(Base):
    __tablename__ = "edition_composition_entries"
    __table_args__ = (
        UniqueConstraint(
            "composition_id", "subject_id", name="uq_edition_composition_entries_subject"
        ),
        Index(
            "uq_edition_composition_entries_included_position",
            "composition_id",
            "position",
            unique=True,
            postgresql_where=text("disposition = 'included'"),
        ),
        CheckConstraint(
            "disposition IN ('included', 'deferred', 'excluded')",
            name="ck_edition_composition_entries_disposition",
        ),
        CheckConstraint(
            "preparation_status IN ('unassessed', 'ready', 'blocked')",
            name="ck_edition_composition_entries_preparation_status",
        ),
        CheckConstraint(
            "position IS NULL OR position >= 1", name="ck_edition_composition_position"
        ),
        CheckConstraint(
            "disposition <> 'included' OR position IS NOT NULL",
            name="ck_edition_composition_included_position",
        ),
        CheckConstraint(
            "(production_run_id IS NULL AND pipeline_generation IS NULL AND artifact_id IS NULL "
            "AND artifact_version IS NULL AND artifact_input_hash IS NULL) OR "
            "(production_run_id IS NOT NULL AND pipeline_generation IS NOT NULL "
            "AND artifact_id IS NOT NULL AND artifact_version IS NOT NULL "
            "AND artifact_input_hash IS NOT NULL)",
            name="ck_edition_composition_artifact_pin_complete",
        ),
        CheckConstraint(
            "disposition <> 'included' OR (production_run_id IS NOT NULL "
            "AND pipeline_generation >= 0 AND artifact_version >= 1 "
            "AND char_length(artifact_input_hash) >= 64 "
            "AND artifact_input_hash ~ '^[0-9a-f]+$')",
            name="ck_edition_composition_included_pin",
        ),
        CheckConstraint(
            "disposition <> 'deferred' OR production_run_id IS NULL",
            name="ck_edition_composition_deferred_without_artifact",
        ),
        CheckConstraint(
            "disposition <> 'excluded' OR review_decision_id IS NOT NULL",
            name="ck_edition_composition_excluded_decision",
        ),
        CheckConstraint(
            "document_sha256 IS NULL OR (char_length(document_sha256) = 64 "
            "AND document_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_edition_composition_document_sha256",
        ),
        Index("ix_edition_composition_entries_run", "production_run_id"),
        Index("ix_edition_composition_entries_artifact", "artifact_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    composition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("edition_compositions.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    position: Mapped[int | None] = mapped_column(nullable=True)
    production_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("production_runs.id", ondelete="RESTRICT")
    )
    pipeline_generation: Mapped[int | None] = mapped_column(nullable=True)
    artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("production_artifacts.id", ondelete="RESTRICT")
    )
    artifact_version: Mapped[int | None] = mapped_column(nullable=True)
    artifact_input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance_batch_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("edition_production_batches.id", ondelete="RESTRICT")
    )
    review_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publication_review_decisions.id", ondelete="RESTRICT")
    )
    document_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preparation_status: Mapped[str] = mapped_column(String(16), nullable=False)


class EditionCompositionEventRow(Base):
    __tablename__ = "edition_composition_events"
    __table_args__ = (
        UniqueConstraint(
            "composition_id", "idempotency_key", name="uq_edition_composition_event_idempotency"
        ),
        CheckConstraint(
            "kind IN ('replace_entries', 'include', 'defer', 'exclude', 'reorder')",
            name="ck_edition_composition_event_kind",
        ),
        CheckConstraint("expected_version >= 0", name="ck_edition_composition_event_expected"),
        CheckConstraint("result_version >= 1", name="ck_edition_composition_event_result"),
        CheckConstraint(
            "char_length(btrim(idempotency_key)) BETWEEN 1 AND 255",
            name="ck_edition_composition_event_idempotency_key",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_edition_composition_event_fingerprint",
        ),
        CheckConstraint(
            "char_length(btrim(actor_id)) > 0", name="ck_edition_composition_event_actor"
        ),
        CheckConstraint(
            "char_length(btrim(correlation_id)) BETWEEN 1 AND 128",
            name="ck_edition_composition_event_correlation",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_edition_composition_event_payload"
        ),
        CheckConstraint(
            "jsonb_typeof(result_snapshot) = 'object'",
            name="ck_edition_composition_event_snapshot",
        ),
        Index("ix_edition_composition_events_edition", "edition_id", "occurred_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    composition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("edition_compositions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_version: Mapped[int] = mapped_column(nullable=False)
    result_version: Mapped[int] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    result_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

"""ORM rows for immutable publication manifests and edition releases."""

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
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PublicationManifestRow(Base):
    __tablename__ = "publication_manifests"
    __table_args__ = (
        CheckConstraint("edition_version >= 1", name="ck_publication_manifest_edition_version"),
        CheckConstraint(
            "schema_version IN ('1', '2')", name="ck_publication_manifest_schema_version"
        ),
        CheckConstraint(
            "(schema_version = '1' AND batch_id IS NOT NULL AND composition_id IS NULL "
            "AND composition_version IS NULL) OR "
            "(schema_version = '2' AND batch_id IS NULL AND composition_id IS NOT NULL "
            "AND composition_version >= 1)",
            name="ck_publication_manifest_schema_scope",
        ),
        CheckConstraint(
            "char_length(content_sha256) = 64 AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_publication_manifest_hash",
        ),
        CheckConstraint(
            "char_length(btrim(created_by)) > 0", name="ck_publication_manifest_creator"
        ),
        Index("ix_publication_manifests_edition_created", "edition_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    edition_version: Mapped[int] = mapped_column(nullable=False)
    schema_version: Mapped[str] = mapped_column(String(1), nullable=False, server_default="1")
    batch_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("edition_production_batches.id", ondelete="RESTRICT"),
        nullable=True,
    )
    composition_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("edition_compositions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    composition_version: Mapped[int | None] = mapped_column(nullable=True)
    manifest_blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublicationManifestEntryRow(Base):
    __tablename__ = "publication_manifest_entries"
    __table_args__ = (
        UniqueConstraint("manifest_id", "position", name="uq_publication_manifest_entry_position"),
        UniqueConstraint("manifest_id", "subject_id", name="uq_publication_manifest_entry_subject"),
        CheckConstraint("position >= 1", name="ck_publication_manifest_entry_position"),
        CheckConstraint(
            "pipeline_generation >= 0", name="ck_publication_manifest_entry_generation"
        ),
        CheckConstraint(
            "document_artifact_version >= 1", name="ck_publication_manifest_entry_artifact_version"
        ),
        CheckConstraint(
            "char_length(document_input_hash) = 64 AND document_input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_publication_manifest_entry_hash",
        ),
        CheckConstraint(
            "document_sha256 IS NULL OR (char_length(document_sha256) = 64 "
            "AND document_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_publication_manifest_entry_document_sha256",
        ),
        Index("ix_publication_manifest_entries_artifact", "document_artifact_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_manifests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(nullable=False)
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    production_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("production_runs.id", ondelete="RESTRICT"),
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
    document_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_batch_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("edition_production_batches.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_review_decisions.id", ondelete="RESTRICT"),
        nullable=True,
    )


class PublicationManifestExclusionRow(Base):
    __tablename__ = "publication_manifest_exclusions"
    __table_args__ = (
        UniqueConstraint(
            "manifest_id", "subject_id", name="uq_publication_manifest_exclusion_subject"
        ),
        UniqueConstraint(
            "manifest_id", "review_decision_id", name="uq_publication_manifest_exclusion_decision"
        ),
        Index("ix_publication_manifest_exclusions_decision", "review_decision_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_manifests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subject_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    review_decision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_review_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )


class EditionReleaseRow(Base):
    __tablename__ = "edition_releases"
    __table_args__ = (
        UniqueConstraint("manifest_id", name="uq_edition_release_manifest"),
        CheckConstraint(
            "char_length(edition_document_sha256) = 64 "
            "AND edition_document_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_edition_release_json_hash",
        ),
        Index("ix_edition_releases_edition", "edition_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    edition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False
    )
    manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_manifests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    edition_document_blob_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blobs.id", ondelete="RESTRICT"), nullable=False
    )
    edition_document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

"""PostgreSQL rows binding a host workspace to canonical investigation records.

Metadata only: the canonical store never receives workspace bytes through these
tables. ``workspace_file_revisions`` is uniquely identified per file, so the
same revision id on another path is a different record; ``task_id`` has no FK
yet (added by a later migration when ``investigation_tasks`` exists).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

WORKSPACE_ORIGIN_VALUES_SQL = "'filesystem', 'ui', 'agent', 'system'"
WORKSPACE_EVENT_KIND_VALUES_SQL = (
    "'file_created', 'file_modified', 'file_renamed', 'file_deleted', "
    "'conflict_created', 'policy_violation', 'sample_materialized', "
    "'sample_rejected', 'scan_completed'"
)
WORKSPACE_EVENT_IMPORT_STATUS_VALUES_SQL = "'received', 'applied', 'ignored', 'rejected'"
WORKSPACE_POLICY_CLASS_VALUES_SQL = (
    "'human_editable', 'agent_owned', 'generated_immutable', 'read_only', 'agent_internal'"
)


class WorkspaceBindingRow(Base):
    """One host workspace, pinned to one investigation and synthesis hash."""

    __tablename__ = "workspace_bindings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "investigation_id",
            name="uq_workspace_bindings_workspace_investigation",
        ),
        UniqueConstraint("investigation_id", name="uq_workspace_bindings_investigation"),
        CheckConstraint(
            "char_length(synthesis_sha256) = 64 AND synthesis_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_workspace_bindings_synthesis_sha256",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "analyst_investigations.id",
            name="fk_workspace_bindings_investigation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    subject_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    delivery_code: Mapped[str] = mapped_column(String(64), nullable=False)
    article_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    synthesis_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkspaceFileRevisionRow(Base):
    """Per-file revision metadata; the row carries no file bytes."""

    __tablename__ = "workspace_file_revisions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "revision_id",
            "path",
            name="uq_workspace_file_revisions_workspace_revision_path",
        ),
        CheckConstraint(
            "char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_workspace_file_revisions_sha256",
        ),
        CheckConstraint("size >= 0", name="ck_workspace_file_revisions_size"),
        CheckConstraint("mtime_ns >= 0", name="ck_workspace_file_revisions_mtime_ns"),
        CheckConstraint(
            f"origin IN ({WORKSPACE_ORIGIN_VALUES_SQL})",
            name="ck_workspace_file_revisions_origin",
        ),
        Index(
            "ix_workspace_file_revisions_workspace_path",
            "workspace_id",
            "path",
            "observed_at",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "workspace_bindings.workspace_id",
            name="fk_workspace_file_revisions_workspace",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    supersedes_revision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class WorkspaceFileEventRow(Base):
    """Append-only agent event stream; ``import_status`` is canonical-only."""

    __tablename__ = "workspace_file_events"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "generation",
            "seq",
            name="uq_workspace_file_events_workspace_generation_seq",
        ),
        CheckConstraint("seq >= 0", name="ck_workspace_file_events_seq"),
        CheckConstraint("generation >= 0", name="ck_workspace_file_events_generation"),
        CheckConstraint("size IS NULL OR size >= 0", name="ck_workspace_file_events_size"),
        CheckConstraint(
            f"kind IN ({WORKSPACE_EVENT_KIND_VALUES_SQL})",
            name="ck_workspace_file_events_kind",
        ),
        CheckConstraint(
            f"origin IN ({WORKSPACE_ORIGIN_VALUES_SQL})",
            name="ck_workspace_file_events_origin",
        ),
        CheckConstraint(
            f"import_status IN ({WORKSPACE_EVENT_IMPORT_STATUS_VALUES_SQL})",
            name="ck_workspace_file_events_import_status",
        ),
        CheckConstraint(
            f"policy_class IS NULL OR policy_class IN ({WORKSPACE_POLICY_CLASS_VALUES_SQL})",
            name="ck_workspace_file_events_policy_class",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "workspace_bindings.workspace_id",
            name="fk_workspace_file_events_workspace",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_revision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    policy_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    import_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'received'")
    )
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceSyncCursorRow(Base):
    """The cursor of one workspace's current generation (one row per workspace)."""

    __tablename__ = "workspace_sync_cursors"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_workspace_sync_cursors_generation"),
        CheckConstraint("cursor >= 0", name="ck_workspace_sync_cursors_cursor"),
    )

    workspace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "workspace_bindings.workspace_id",
            name="fk_workspace_sync_cursors_workspace",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cursor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_scan_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

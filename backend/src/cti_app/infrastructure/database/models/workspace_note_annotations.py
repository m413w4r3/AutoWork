"""Append-only human note annotation rows (M24-WS2, ADR-0011 D6).

One row records one human action on one per-file workspace revision: a link to
a task, an IOC value or a sample of the same investigation, a useful/unuseful
mark, an explicit author attribution, or a dismissal.  The table is never a
second evidence store: no row creates an observation, a finding or a sample
qualification.  The composite foreign keys pin the workspace revision per file
and the task to the same investigation; the service additionally resolves the
IOC and sample membership because those aggregates are subject-scoped.

UPDATE, DELETE and TRUNCATE are refused by triggers, so a later mark or a
dismissal is a new row and the current note view is derived by ordering.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

NOTE_ANNOTATION_KIND_VALUES_SQL = (
    "'link_task', 'link_ioc', 'link_sample', 'mark_useful', 'unmark_useful', "
    "'attribute_author', 'dismiss'"
)
NOTE_ANNOTATION_IOC_TYPE_VALUES_SQL = "'md5', 'sha1', 'sha256', 'ipv4', 'domain', 'url'"


class WorkspaceNoteAnnotationRow(Base):
    """One immutable annotation of one workspace note revision."""

    __tablename__ = "workspace_note_annotations"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_workspace_note_annotations"),
        UniqueConstraint(
            "investigation_id",
            "idempotency_key",
            name="uq_workspace_note_annotations_investigation_idempotency",
        ),
        ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_workspace_note_annotations_investigation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "investigation_id"],
            ["workspace_bindings.workspace_id", "workspace_bindings.investigation_id"],
            name="fk_workspace_note_annotations_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "revision_id", "file_relpath"],
            [
                "workspace_file_revisions.workspace_id",
                "workspace_file_revisions.revision_id",
                "workspace_file_revisions.path",
            ],
            name="fk_workspace_note_annotations_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_workspace_note_annotations_task",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"kind IN ({NOTE_ANNOTATION_KIND_VALUES_SQL})",
            name="ck_workspace_note_annotations_kind",
        ),
        CheckConstraint(
            "actor_kind = 'human'",
            name="ck_workspace_note_annotations_actor_kind",
        ),
        CheckConstraint(
            "btrim(actor_id) <> '' AND char_length(actor_id) <= 255",
            name="ck_workspace_note_annotations_actor_id",
        ),
        CheckConstraint(
            (
                "file_relpath LIKE 'notes/%' "
                "AND file_relpath !~ '(^|/)\\.\\.?(/|$)' "
                "AND char_length(file_relpath) <= 4096"
            ),
            name="ck_workspace_note_annotations_note_path",
        ),
        CheckConstraint(
            f"ioc_type IS NULL OR ioc_type IN ({NOTE_ANNOTATION_IOC_TYPE_VALUES_SQL})",
            name="ck_workspace_note_annotations_ioc_type",
        ),
        CheckConstraint(
            "ioc_value IS NULL OR (btrim(ioc_value) <> '' AND char_length(ioc_value) <= 2048)",
            name="ck_workspace_note_annotations_ioc_value",
        ),
        CheckConstraint(
            "(ioc_value IS NULL) = (ioc_type IS NULL)",
            name="ck_workspace_note_annotations_ioc_pair",
        ),
        CheckConstraint(
            "detail IS NULL OR (btrim(detail) <> '' AND char_length(detail) <= 4000)",
            name="ck_workspace_note_annotations_detail",
        ),
        CheckConstraint(
            "btrim(idempotency_key) <> '' AND char_length(idempotency_key) <= 255",
            name="ck_workspace_note_annotations_idempotency_key",
        ),
        CheckConstraint(
            "(kind = 'link_task' AND target_task_id IS NOT NULL AND "
            "ioc_value IS NULL AND target_sample_id IS NULL AND detail IS NULL) OR "
            "(kind = 'link_ioc' AND target_task_id IS NULL AND "
            "ioc_value IS NOT NULL AND ioc_type IS NOT NULL AND "
            "target_sample_id IS NULL AND detail IS NULL) OR "
            "(kind = 'link_sample' AND target_task_id IS NULL AND ioc_value IS NULL AND "
            "target_sample_id IS NOT NULL AND detail IS NULL) OR "
            "(kind = 'attribute_author' AND target_task_id IS NULL AND ioc_value IS NULL AND "
            "target_sample_id IS NULL AND detail IS NOT NULL) OR "
            "(kind = 'dismiss' AND target_task_id IS NULL AND ioc_value IS NULL AND "
            "target_sample_id IS NULL AND detail IS NOT NULL) OR "
            "(kind IN ('mark_useful', 'unmark_useful') AND target_task_id IS NULL AND "
            "ioc_value IS NULL AND target_sample_id IS NULL AND detail IS NULL)",
            name="ck_workspace_note_annotations_targets",
        ),
        Index(
            "ix_workspace_note_annotations_investigation_path",
            "investigation_id",
            "file_relpath",
            "created_at",
            "id",
        ),
        Index(
            "ix_workspace_note_annotations_revision",
            "workspace_id",
            "revision_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    file_relpath: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ioc_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    ioc_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_sample_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

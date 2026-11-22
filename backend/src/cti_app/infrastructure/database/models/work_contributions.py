"""Append-only workspace contribution rows (M24-07, ADR-0011 D2/D6).

A row is the provenance of one workspace file revision, never a second
evidence store.  The ``(workspace_id, workspace_revision_id, file_relpath)``
foreign key pins the exact per-file revision; the partial unique index allows
one non-rejected import per revision while a rejected row stays terminal and
auditable.  Triggers installed by ``0024_work_contributions`` refuse every
UPDATE, DELETE and TRUNCATE.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

WORK_CONTRIBUTION_CATEGORY_VALUES_SQL = "'work_contribution', 'observation', 'finding'"
WORK_CONTRIBUTION_STATUS_VALUES_SQL = "'imported', 'rejected', 'skipped'"
WORK_CONTRIBUTION_ORIGIN_VALUES_SQL = "'human', 'agent', 'host'"
WORK_CONTRIBUTION_SCHEMA_VERSION_SQL = "'work-contribution.v1'"
WORK_CONTRIBUTION_ACTIVE_REVISION_PREDICATE = "import_status <> 'rejected'"


def _sha256_sql(column: str) -> str:
    return f"char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


class WorkContributionRow(Base):
    __tablename__ = "work_contributions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_work_contributions"),
        UniqueConstraint(
            "investigation_id",
            "idempotency_key",
            name="uq_work_contributions_investigation_idempotency",
        ),
        ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_work_contributions_investigation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "investigation_id"],
            ["workspace_bindings.workspace_id", "workspace_bindings.investigation_id"],
            name="fk_work_contributions_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_work_contributions_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "workspace_revision_id", "file_relpath"],
            [
                "workspace_file_revisions.workspace_id",
                "workspace_file_revisions.revision_id",
                "workspace_file_revisions.path",
            ],
            name="fk_work_contributions_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["content_blob_id"],
            ["blobs.id"],
            name="fk_work_contributions_content_blob",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["observation_id"],
            ["evidence_observations.id"],
            name="fk_work_contributions_observation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["finding_id"],
            ["investigation_finding_versions.id"],
            name="fk_work_contributions_finding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["sample_association_id"],
            ["sample_associations.id"],
            name="fk_work_contributions_sample_association",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"category IN ({WORK_CONTRIBUTION_CATEGORY_VALUES_SQL})",
            name="ck_work_contributions_category",
        ),
        CheckConstraint(
            f"schema_version = {WORK_CONTRIBUTION_SCHEMA_VERSION_SQL}",
            name="ck_work_contributions_schema_version",
        ),
        CheckConstraint(
            f"import_status IN ({WORK_CONTRIBUTION_STATUS_VALUES_SQL})",
            name="ck_work_contributions_import_status",
        ),
        CheckConstraint(
            f"origin IN ({WORK_CONTRIBUTION_ORIGIN_VALUES_SQL})",
            name="ck_work_contributions_origin",
        ),
        CheckConstraint(_sha256_sql("file_sha256"), name="ck_work_contributions_file_sha256"),
        CheckConstraint(
            f"content_sha256 IS NULL OR ({_sha256_sql('content_sha256')})",
            name="ck_work_contributions_content_sha256",
        ),
        CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 = file_sha256",
            name="ck_work_contributions_content_hash_matches_file",
        ),
        CheckConstraint(
            "file_relpath <> '' AND file_relpath !~ '(^/|(^|/)\\.\\.(/|$))'",
            name="ck_work_contributions_relpath_relative",
        ),
        CheckConstraint(
            "(import_status = 'imported') = (validation_error_code IS NULL)",
            name="ck_work_contributions_reverse_check",
        ),
        CheckConstraint(
            "(origin <> 'agent') OR "
            "(task_id IS NOT NULL AND lease_fence IS NOT NULL AND lease_fence >= 1)",
            name="ck_work_contributions_agent_authority",
        ),
        CheckConstraint(
            "origin = 'agent' OR lease_fence IS NULL",
            name="ck_work_contributions_non_agent_no_lease",
        ),
        CheckConstraint(
            "import_status <> 'imported' OR "
            "(content_blob_id IS NOT NULL AND content_sha256 IS NOT NULL)",
            name="ck_work_contributions_imported_content",
        ),
        CheckConstraint(
            "("
            "(import_status = 'imported' AND category = 'work_contribution' AND "
            "observation_id IS NULL AND finding_id IS NULL AND sample_association_id IS NULL)"
            " OR (import_status = 'imported' AND category = 'observation' AND "
            "observation_id IS NOT NULL AND finding_id IS NULL AND sample_association_id IS NULL)"
            " OR (import_status = 'imported' AND category = 'finding' AND "
            "finding_id IS NOT NULL AND observation_id IS NULL)"
            " OR (import_status <> 'imported' AND observation_id IS NULL AND "
            "finding_id IS NULL AND sample_association_id IS NULL)"
            ")",
            name="ck_work_contributions_category_refs",
        ),
        CheckConstraint(
            "sample_association_id IS NULL OR "
            "(category = 'finding' AND import_status = 'imported')",
            name="ck_work_contributions_sample_ref",
        ),
        Index(
            "uq_work_contributions_active_revision",
            "workspace_id",
            "workspace_revision_id",
            unique=True,
            postgresql_where=text(WORK_CONTRIBUTION_ACTIVE_REVISION_PREDICATE),
        ),
        Index(
            "ix_work_contributions_investigation_created",
            "investigation_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    file_relpath: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_fence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_blob_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    import_status: Mapped[str] = mapped_column(String(16), nullable=False)
    observation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    finding_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sample_association_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    validation_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

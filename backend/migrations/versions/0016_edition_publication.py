"""Persist immutable publication freezes and deterministic edition releases."""

from alembic import op
import sqlalchemy as sa


revision = "0016_edition_publication"
down_revision = "0015_nullable_review_document"
branch_labels = None
depends_on = None


_HASH = "char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


def upgrade() -> None:
    op.create_table(
        "publication_manifests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("edition_version", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_blob_id", sa.Uuid(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("edition_version >= 1", name="ck_publication_manifest_edition_version"),
        sa.CheckConstraint(
            _HASH.format(column="content_sha256"), name="ck_publication_manifest_hash"
        ),
        sa.CheckConstraint(
            "char_length(btrim(created_by)) > 0", name="ck_publication_manifest_creator"
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["editions.id"],
            name="fk_publication_manifest_edition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["edition_production_batches.id"],
            name="fk_publication_manifest_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_blob_id"],
            ["blobs.id"],
            name="fk_publication_manifest_blob",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "edition_id", "edition_version", name="uq_publication_manifest_edition_version"
        ),
    )
    op.create_index(
        "ix_publication_manifests_edition_created",
        "publication_manifests",
        ["edition_id", "created_at", "id"],
    )

    op.create_table(
        "publication_manifest_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_generation", sa.Integer(), nullable=False),
        sa.Column("document_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("document_artifact_version", sa.Integer(), nullable=False),
        sa.Column("document_input_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("position >= 1", name="ck_publication_manifest_entry_position"),
        sa.CheckConstraint(
            "pipeline_generation >= 0", name="ck_publication_manifest_entry_generation"
        ),
        sa.CheckConstraint(
            "document_artifact_version >= 1",
            name="ck_publication_manifest_entry_artifact_version",
        ),
        sa.CheckConstraint(
            _HASH.format(column="document_input_hash"), name="ck_publication_manifest_entry_hash"
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["publication_manifests.id"],
            name="fk_publication_manifest_entry_manifest",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["subjects.id"],
            name="fk_publication_manifest_entry_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["subject_production_runs.id"],
            name="fk_publication_manifest_entry_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_artifact_id"],
            ["production_artifacts.id"],
            name="fk_publication_manifest_entry_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "manifest_id", "position", name="uq_publication_manifest_entry_position"
        ),
        sa.UniqueConstraint(
            "manifest_id", "subject_id", name="uq_publication_manifest_entry_subject"
        ),
    )
    op.create_index(
        "ix_publication_manifest_entries_artifact",
        "publication_manifest_entries",
        ["document_artifact_id"],
    )

    op.create_table(
        "publication_manifest_exclusions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("review_decision_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["publication_manifests.id"],
            name="fk_publication_manifest_exclusion_manifest",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["subjects.id"],
            name="fk_publication_manifest_exclusion_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_decision_id"],
            ["publication_review_decisions.id"],
            name="fk_publication_manifest_exclusion_decision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "manifest_id", "subject_id", name="uq_publication_manifest_exclusion_subject"
        ),
        sa.UniqueConstraint(
            "manifest_id", "review_decision_id", name="uq_publication_manifest_exclusion_decision"
        ),
    )
    op.create_index(
        "ix_publication_manifest_exclusions_decision",
        "publication_manifest_exclusions",
        ["review_decision_id"],
    )

    op.create_table(
        "edition_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("edition_document_blob_id", sa.Uuid(), nullable=False),
        sa.Column("markdown_blob_id", sa.Uuid(), nullable=False),
        sa.Column("docx_blob_id", sa.Uuid(), nullable=False),
        sa.Column("edition_document_sha256", sa.String(length=64), nullable=False),
        sa.Column("markdown_sha256", sa.String(length=64), nullable=False),
        sa.Column("docx_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _HASH.format(column="edition_document_sha256"), name="ck_edition_release_json_hash"
        ),
        sa.CheckConstraint(
            _HASH.format(column="markdown_sha256"), name="ck_edition_release_markdown_hash"
        ),
        sa.CheckConstraint(_HASH.format(column="docx_sha256"), name="ck_edition_release_docx_hash"),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name="fk_edition_release_edition", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["publication_manifests.id"],
            name="fk_edition_release_manifest",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edition_document_blob_id"],
            ["blobs.id"],
            name="fk_edition_release_json_blob",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["markdown_blob_id"],
            ["blobs.id"],
            name="fk_edition_release_markdown_blob",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["docx_blob_id"], ["blobs.id"], name="fk_edition_release_docx_blob", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manifest_id", name="uq_edition_release_manifest"),
    )
    op.create_index("ix_edition_releases_edition", "edition_releases", ["edition_id", "created_at"])

    for table, trigger in (
        ("publication_manifests", "trg_publication_manifests_append_only"),
        ("publication_manifest_entries", "trg_publication_manifest_entries_append_only"),
        ("publication_manifest_exclusions", "trg_publication_manifest_exclusions_append_only"),
        ("edition_releases", "trg_edition_releases_append_only"),
    ):
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
        )


def downgrade() -> None:
    for table, trigger in (
        ("edition_releases", "trg_edition_releases_append_only"),
        ("publication_manifest_exclusions", "trg_publication_manifest_exclusions_append_only"),
        ("publication_manifest_entries", "trg_publication_manifest_entries_append_only"),
        ("publication_manifests", "trg_publication_manifests_append_only"),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    op.drop_index("ix_edition_releases_edition", table_name="edition_releases")
    op.drop_table("edition_releases")
    op.drop_index(
        "ix_publication_manifest_exclusions_decision", table_name="publication_manifest_exclusions"
    )
    op.drop_table("publication_manifest_exclusions")
    op.drop_index(
        "ix_publication_manifest_entries_artifact", table_name="publication_manifest_entries"
    )
    op.drop_table("publication_manifest_entries")
    op.drop_index("ix_publication_manifests_edition_created", table_name="publication_manifests")
    op.drop_table("publication_manifests")

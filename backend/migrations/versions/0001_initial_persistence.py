"""Initial canonical persistence, provenance and blob catalog.

Revision ID: 0001
Revises:
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TLP_CHECK = "tlp IN ('CLEAR', 'GREEN', 'AMBER', 'AMBER+STRICT', 'RED')"


def upgrade() -> None:
    op.create_table(
        "blobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("logical_bucket", sa.String(length=63), nullable=False),
        sa.Column("object_key", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size >= 0", name="ck_blobs_size_non_negative"),
        sa.CheckConstraint("char_length(sha256) = 64", name="ck_blobs_sha256_length"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_blobs_sha256_format"),
        sa.CheckConstraint(
            "logical_bucket ~ '^[a-z0-9][a-z0-9._-]{0,62}$'",
            name="ck_blobs_logical_bucket_format",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("logical_bucket", "sha256", name="uq_blobs_bucket_sha256"),
        sa.UniqueConstraint("object_key", name="uq_blobs_object_key"),
    )
    op.create_table(
        "subjects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name="ck_subjects_tlp"),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_subjects_slug_format"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id"),
        sa.UniqueConstraint("slug"),
    )
    _create_asset_table("source_documents")
    _create_asset_table("samples")
    op.create_table(
        "provenance_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name="ck_provenance_events_tlp"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provenance_events_aggregate",
        "provenance_events",
        ["aggregate_type", "aggregate_id", "occurred_at"],
    )
    op.create_index("ix_provenance_events_subject_id", "provenance_events", ["subject_id"])
    _install_tlp_guard()
    _install_provenance_guard()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_provenance_events_append_only ON provenance_events")
    op.execute("DROP FUNCTION IF EXISTS reject_provenance_mutation()")
    for table in ("subjects", "source_documents", "samples"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_prevent_tlp_downgrade ON {table}")
    op.execute("DROP FUNCTION IF EXISTS prevent_tlp_downgrade()")
    op.drop_index("ix_provenance_events_subject_id", table_name="provenance_events")
    op.drop_index("ix_provenance_events_aggregate", table_name="provenance_events")
    op.drop_table("provenance_events")
    op.drop_index("ix_samples_blob_id", table_name="samples")
    op.drop_index("ix_samples_subject_id", table_name="samples")
    op.drop_table("samples")
    op.drop_index("ix_source_documents_blob_id", table_name="source_documents")
    op.drop_index("ix_source_documents_subject_id", table_name="source_documents")
    op.drop_table("source_documents")
    op.drop_table("subjects")
    op.drop_table("blobs")


def _create_asset_table(table_name: str) -> None:
    op.create_table(
        table_name,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("license_restriction", sa.Text(), nullable=True),
        sa.Column("tlp", sa.String(length=16), nullable=False),
        sa.Column("do_not_submit", sa.Boolean(), nullable=False),
        sa.Column("external_llm_allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(TLP_CHECK, name=f"ck_{table_name}_tlp"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{table_name}_subject_id", table_name, ["subject_id"])
    op.create_index(f"ix_{table_name}_blob_id", table_name, ["blob_id"])


def _install_tlp_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION prevent_tlp_downgrade() RETURNS trigger AS $$
        DECLARE
            old_rank integer;
            new_rank integer;
        BEGIN
            old_rank := CASE OLD.tlp
                WHEN 'CLEAR' THEN 0 WHEN 'GREEN' THEN 1 WHEN 'AMBER' THEN 2
                WHEN 'AMBER+STRICT' THEN 3 WHEN 'RED' THEN 4 END;
            new_rank := CASE NEW.tlp
                WHEN 'CLEAR' THEN 0 WHEN 'GREEN' THEN 1 WHEN 'AMBER' THEN 2
                WHEN 'AMBER+STRICT' THEN 3 WHEN 'RED' THEN 4 END;
            IF new_rank < old_rank THEN
                RAISE EXCEPTION 'TLP downgrade from % to % is forbidden', OLD.tlp, NEW.tlp
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("subjects", "source_documents", "samples"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_prevent_tlp_downgrade
            BEFORE UPDATE OF tlp ON {table}
            FOR EACH ROW EXECUTE FUNCTION prevent_tlp_downgrade()
            """
        )


def _install_provenance_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_provenance_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'provenance_events is append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provenance_events_append_only
        BEFORE UPDATE OR DELETE ON provenance_events
        FOR EACH ROW EXECUTE FUNCTION reject_provenance_mutation()
        """
    )

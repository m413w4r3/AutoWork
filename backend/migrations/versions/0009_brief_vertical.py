"""Add immutable brief evidence packs and versioned drafts.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_human_decisions_type", "human_decisions", type_="check")
    op.create_check_constraint(
        "ck_human_decisions_type",
        "human_decisions",
        "decision_type IN ('merge', 'split', 'reject', 'select', 'claim_validate', "
        "'claim_correct', 'claim_reject', 'indicator_validate', 'indicator_correct', "
        "'indicator_reject', 'source_relationship_validate', "
        "'source_relationship_correct', 'brief_changes_requested', 'brief_approve', "
        "'brief_promote')",
    )
    op.create_table(
        "brief_evidence_packs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("object_hashes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("claims", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("indicators", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalized_entities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uncertainties", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("human_decisions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_brief_evidence_packs_version"),
        sa.CheckConstraint(
            "char_length(content_hash) = 64 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_brief_evidence_packs_hash",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_id", "version", name="uq_brief_evidence_packs_version"),
        sa.UniqueConstraint(
            "subject_id", "content_hash", name="uq_brief_evidence_packs_content_hash"
        ),
    )
    op.create_index(
        "ix_brief_evidence_packs_subject",
        "brief_evidence_packs",
        ["subject_id", "version"],
    )
    op.create_table(
        "brief_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("pack_id", sa.Uuid(), nullable=False),
        sa.Column("pack_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("blocks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_run_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("parent_draft_id", sa.Uuid(), nullable=True),
        sa.Column("regenerated_block_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_brief_drafts_version"),
        sa.CheckConstraint(
            "status IN ('draft', 'changes_requested', 'approved', 'promoted')",
            name="ck_brief_drafts_status",
        ),
        sa.CheckConstraint(
            "char_length(pack_hash) = 64 AND pack_hash ~ '^[0-9a-f]{64}$'",
            name="ck_brief_drafts_pack_hash",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["editorial_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pack_id"], ["brief_evidence_packs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_draft_id"], ["brief_drafts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_id", "version", name="uq_brief_drafts_version"),
    )
    op.create_index("ix_brief_drafts_subject", "brief_drafts", ["subject_id", "version"])
    for table in ("brief_evidence_packs", "brief_drafts"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()
            """
        )


def downgrade() -> None:
    for table in ("brief_drafts", "brief_evidence_packs"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.drop_index("ix_brief_drafts_subject", table_name="brief_drafts")
    op.drop_table("brief_drafts")
    op.drop_index("ix_brief_evidence_packs_subject", table_name="brief_evidence_packs")
    op.drop_table("brief_evidence_packs")
    op.drop_constraint("ck_human_decisions_type", "human_decisions", type_="check")
    op.create_check_constraint(
        "ck_human_decisions_type",
        "human_decisions",
        "decision_type IN ('merge', 'split', 'reject', 'select', 'claim_validate', "
        "'claim_correct', 'claim_reject', 'indicator_validate', 'indicator_correct', "
        "'indicator_reject', 'source_relationship_validate', "
        "'source_relationship_correct')",
    )

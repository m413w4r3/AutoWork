"""Add editorial preservation tables and columns for Increment 3.

This migration adds:
1. New columns to brief_evidence_packs for coverage tracking
2. Table for brief_amendments
3. Table for editorial_update_decisions (append-only log)

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add new columns to brief_evidence_packs
    op.add_column(
        "brief_evidence_packs",
        sa.Column("built_from_snapshot_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "brief_evidence_packs",
        sa.Column("built_from_snapshot_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "brief_evidence_packs",
        sa.Column(
            "covered_contribution_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=True,
        ),
    )
    op.add_column(
        "brief_evidence_packs",
        sa.Column("scope", sa.String(length=10), nullable=True, server_default="full"),
    )
    op.add_column(
        "brief_evidence_packs",
        sa.Column("base_pack_id", sa.Uuid(), nullable=True),
    )

    # Add foreign key constraints for snapshot_id and base_pack_id
    op.create_foreign_key(
        "fk_brief_evidence_packs_snapshot",
        "brief_evidence_packs",
        "discovery_snapshots",
        ["built_from_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_brief_evidence_packs_base",
        "brief_evidence_packs",
        "brief_evidence_packs",
        ["base_pack_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Create brief_amendments table
    op.create_table(
        "brief_amendments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("parent_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("root_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("evidence_pack_id", sa.Uuid(), nullable=False),
        sa.Column(
            "contribution_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=False,
        ),
        sa.Column("draft_id", sa.Uuid(), nullable=True),
        sa.Column("revision_reason", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["discovery_subject_identities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_artifact_id"], ["brief_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["root_artifact_id"], ["brief_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["trigger_snapshot_id"], ["discovery_snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_pack_id"], ["brief_evidence_packs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_brief_amendments_edition", "brief_amendments", ["edition_id"])
    op.create_index("ix_brief_amendments_subject", "brief_amendments", ["subject_id"])
    op.create_index("ix_brief_amendments_status", "brief_amendments", ["status"])

    # Create editorial_update_decisions table (append-only log)
    op.create_table(
        "editorial_update_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("edition_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column(
            "contribution_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("supersedes_decision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["edition_id"], ["editions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["artifact_id"], ["brief_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_decision_id"], ["editorial_update_decisions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_editorial_update_decisions_edition", "editorial_update_decisions", ["edition_id"])
    op.create_index("ix_editorial_update_decisions_artifact", "editorial_update_decisions", ["artifact_id"])
    op.create_index("ix_editorial_update_decisions_action", "editorial_update_decisions", ["action"])


def downgrade() -> None:
    # Drop tables in reverse order
    op.drop_table("editorial_update_decisions")
    op.drop_table("brief_amendments")

    # Remove columns from brief_evidence_packs
    op.drop_constraint("fk_brief_evidence_packs_base", "brief_evidence_packs", type_="foreignkey")
    op.drop_constraint("fk_brief_evidence_packs_snapshot", "brief_evidence_packs", type_="foreignkey")
    op.drop_column("brief_evidence_packs", "base_pack_id")
    op.drop_column("brief_evidence_packs", "scope")
    op.drop_column("brief_evidence_packs", "covered_contribution_ids")
    op.drop_column("brief_evidence_packs", "built_from_snapshot_version")
    op.drop_column("brief_evidence_packs", "built_from_snapshot_id")

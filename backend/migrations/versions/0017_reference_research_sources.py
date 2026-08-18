"""Make source collections self-sufficient and freeze the research date.

A source found during reference research (Q1) has no DiscoveryBatch candidate to
read its metadata from, so `source_collections` gains an origin kind, a
canonical URL and its own metadata snapshot, and the discovery foreign keys
become optional.

`subject_production_runs.research_date` freezes the date used to reject
impossible publication dates, so a retry after midnight cannot shift it.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_collections", sa.Column("origin_kind", sa.String(32)))
    op.add_column("source_collections", sa.Column("canonical_url", sa.Text()))
    op.add_column("source_collections", sa.Column("title", sa.Text()))
    op.add_column("source_collections", sa.Column("publisher", sa.Text()))
    op.add_column("source_collections", sa.Column("published_at", sa.Date()))
    op.add_column("source_collections", sa.Column("source_tlp", sa.String(32)))
    op.add_column("source_collections", sa.Column("sensitivity", sa.String(32)))
    op.add_column("source_collections", sa.Column("external_llm_allowed", sa.Boolean()))
    op.add_column("source_collections", sa.Column("do_not_submit", sa.Boolean()))

    # Existing rows all came from discovery, and `requested_url` was already
    # stored canonicalised by the collection service.
    op.execute(
        """
        UPDATE source_collections
        SET origin_kind = 'discovery',
            canonical_url = requested_url,
            source_tlp = 'CLEAR',
            sensitivity = 'public',
            external_llm_allowed = TRUE,
            do_not_submit = FALSE
        """
    )

    for column in (
        "origin_kind",
        "canonical_url",
        "source_tlp",
        "sensitivity",
        "external_llm_allowed",
        "do_not_submit",
    ):
        op.alter_column("source_collections", column, nullable=False)

    # Reference-research sources have neither of these.
    op.alter_column("source_collections", "batch_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column(
        "source_collections", "source_candidate_id", existing_type=sa.Uuid(), nullable=True
    )

    op.create_check_constraint(
        "ck_source_collections_origin_kind",
        "source_collections",
        "origin_kind IN ('discovery', 'reference_research', 'manual')",
    )
    # One collection per subject and per canonical URL, whatever its origin:
    # this is what stops Q1 from re-adding a publication already collected.
    op.create_unique_constraint(
        "uq_source_collections_subject_canonical_url",
        "source_collections",
        ["subject_id", "canonical_url"],
    )

    op.add_column("subject_production_runs", sa.Column("research_date", sa.Date()))


def downgrade() -> None:
    op.drop_column("subject_production_runs", "research_date")
    op.drop_constraint(
        "uq_source_collections_subject_canonical_url", "source_collections", type_="unique"
    )
    op.drop_constraint("ck_source_collections_origin_kind", "source_collections", type_="check")
    op.alter_column("source_collections", "batch_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column(
        "source_collections", "source_candidate_id", existing_type=sa.Uuid(), nullable=False
    )
    for column in (
        "do_not_submit",
        "external_llm_allowed",
        "sensitivity",
        "source_tlp",
        "published_at",
        "publisher",
        "title",
        "canonical_url",
        "origin_kind",
    ):
        op.drop_column("source_collections", column)

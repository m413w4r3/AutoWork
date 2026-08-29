"""Allow document-less exclusions for failed production runs."""

from alembic import op
import sqlalchemy as sa


revision = "0015_nullable_review_document"
down_revision = "0014_publication_review"
branch_labels = None
depends_on = None


_DOCUMENT_IDENTITY = (
    "(document_artifact_id IS NULL AND document_artifact_version IS NULL "
    "AND document_input_hash IS NULL) OR "
    "(document_artifact_id IS NOT NULL AND document_artifact_version IS NOT NULL "
    "AND document_input_hash IS NOT NULL)"
)


def upgrade() -> None:
    op.alter_column(
        "publication_review_decisions",
        "document_artifact_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.alter_column(
        "publication_review_decisions",
        "document_artifact_version",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.alter_column(
        "publication_review_decisions",
        "document_input_hash",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_publication_review_document_identity",
        "publication_review_decisions",
        _DOCUMENT_IDENTITY,
    )
    op.create_check_constraint(
        "ck_publication_review_include_document_identity",
        "publication_review_decisions",
        "decision <> 'include' OR (document_artifact_id IS NOT NULL "
        "AND document_artifact_version IS NOT NULL AND document_input_hash IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_publication_review_include_document_identity",
        "publication_review_decisions",
        type_="check",
    )
    op.drop_constraint(
        "ck_publication_review_document_identity",
        "publication_review_decisions",
        type_="check",
    )
    op.alter_column(
        "publication_review_decisions",
        "document_input_hash",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.alter_column(
        "publication_review_decisions",
        "document_artifact_version",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "publication_review_decisions",
        "document_artifact_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )

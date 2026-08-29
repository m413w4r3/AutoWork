"""real AutoWork reference corpus"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_reference_corpus"
down_revision = "0007_goodware_baselines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sample_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("samples.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sample_sha256", sa.String(64), nullable=False),
        sa.Column("family_label", sa.Text(), nullable=False),
        sa.Column(
            "origin_investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analyst_investigations.id", ondelete="RESTRICT"),
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("label_source", sa.String(32), nullable=False),
        sa.UniqueConstraint("sample_id", "family_label", name="uq_reference_members_sample_label"),
        sa.CheckConstraint(
            "label_source IN ('ANALYST','OPERATOR_IMPORT')", name="ck_reference_members_source"
        ),
    )
    op.create_index("ix_reference_members_sample_id", "reference_members", ["sample_id"])
    op.create_table(
        "reference_member_disputes",
        sa.Column(
            "member_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_members.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
    )
    op.execute(
        """
        CREATE FUNCTION forbid_reference_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'reference corpus rows are append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER reference_members_immutable
        BEFORE UPDATE OR DELETE ON reference_members
        FOR EACH ROW
        EXECUTE FUNCTION forbid_reference_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER reference_member_disputes_immutable
        BEFORE UPDATE OR DELETE ON reference_member_disputes
        FOR EACH ROW
        EXECUTE FUNCTION forbid_reference_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER reference_member_disputes_immutable ON reference_member_disputes")
    op.execute("DROP TRIGGER reference_members_immutable ON reference_members")
    op.execute("DROP FUNCTION forbid_reference_mutation()")
    op.drop_table("reference_member_disputes")
    op.drop_index("ix_reference_members_sample_id", table_name="reference_members")
    op.drop_table("reference_members")

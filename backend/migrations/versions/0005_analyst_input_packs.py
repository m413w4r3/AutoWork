"""Persist immutable analyst input-pack references and VT file features."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_analyst_input_packs"
down_revision: str | None = "0004_sample_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analyst_input_packs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_analyst_input_pack_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["analyst_investigations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["blob_id"], ["blobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("investigation_id", name="uq_analyst_input_packs_investigation"),
    )
    op.execute(
        "CREATE TRIGGER trg_analyst_input_packs_append_only BEFORE UPDATE OR DELETE "
        "ON analyst_input_packs FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation()"
    )
    for name in ("vhash", "imphash", "ssdeep", "tlsh", "main_icon_dhash", "rich_header_hash"):
        op.add_column(
            "virustotal_file_views", sa.Column(name, sa.String(length=255), nullable=True)
        )


def downgrade() -> None:
    for name in ("vhash", "imphash", "ssdeep", "tlsh", "main_icon_dhash", "rich_header_hash"):
        op.drop_column("virustotal_file_views", name)
    op.execute("DROP TRIGGER trg_analyst_input_packs_append_only ON analyst_input_packs")
    op.drop_table("analyst_input_packs")

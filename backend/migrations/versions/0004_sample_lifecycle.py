"""Add typed sample lifecycle, provenance and local/VT feature keys."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_sample_lifecycle"
down_revision: str | None = "0003_analyst_investigation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "samples",
        sa.Column(
            "origin_kind", sa.String(length=32), nullable=False, server_default="source_seed"
        ),
    )
    op.add_column(
        "samples",
        sa.Column("state", sa.String(length=32), nullable=False, server_default="validated"),
    )
    op.add_column("samples", sa.Column("source_service", sa.Text(), nullable=True))
    op.add_column("samples", sa.Column("source_object_id", sa.Text(), nullable=True))
    op.add_column("samples", sa.Column("expected_hash", sa.String(length=64), nullable=True))
    op.add_column("samples", sa.Column("validation_actor", sa.String(length=255), nullable=True))
    op.add_column(
        "samples", sa.Column("validation_date", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("samples", sa.Column("validation_reason", sa.Text(), nullable=True))
    for name in ("imphash", "ssdeep", "tlsh", "rich_header_hash", "vhash", "main_icon_dhash"):
        op.add_column("samples", sa.Column(name, sa.String(length=255), nullable=True))
        op.add_column("samples", sa.Column(f"{name}_source", sa.String(length=8), nullable=True))
        op.create_index(f"ix_samples_{name}", "samples", [name])
    op.create_check_constraint(
        "ck_samples_origin_kind",
        "samples",
        "origin_kind IN ('source_seed','vt_seed','vt_hunt_hit','benign_reference','manual')",
    )
    op.create_check_constraint(
        "ck_samples_state",
        "samples",
        "state IN ('quarantined','review_candidate','validated','rejected')",
    )
    op.create_check_constraint(
        "ck_samples_expected_hash",
        "samples",
        "expected_hash IS NULL OR (char_length(expected_hash) = 64 AND "
        "expected_hash ~ '^[0-9a-f]{64}$')",
    )
    for name in ("imphash", "ssdeep", "tlsh", "rich_header_hash", "vhash", "main_icon_dhash"):
        op.create_check_constraint(
            f"ck_samples_{name}_source",
            "samples",
            f"{name}_source IS NULL OR {name}_source IN ('local','vt')",
        )
    op.alter_column("samples", "origin_kind", server_default=None)
    op.alter_column("samples", "state", server_default=None)


def downgrade() -> None:
    for name in ("imphash", "ssdeep", "tlsh", "rich_header_hash", "vhash", "main_icon_dhash"):
        op.drop_constraint(f"ck_samples_{name}_source", "samples", type_="check")
        op.drop_index(f"ix_samples_{name}", table_name="samples")
        op.drop_column("samples", f"{name}_source")
        op.drop_column("samples", name)
    op.drop_constraint("ck_samples_expected_hash", "samples", type_="check")
    op.drop_constraint("ck_samples_state", "samples", type_="check")
    op.drop_constraint("ck_samples_origin_kind", "samples", type_="check")
    for name in (
        "validation_reason",
        "validation_date",
        "validation_actor",
        "expected_hash",
        "source_object_id",
        "source_service",
        "state",
        "origin_kind",
    ):
        op.drop_column("samples", name)

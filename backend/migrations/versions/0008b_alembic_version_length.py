"""Allow Alembic to store the historical 0009 revision id.

Revision ID: 0008b_version_length
Revises: 0008_production_routing
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008b_version_length"
down_revision: str | None = "0008_production_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _version_column_length() -> int:
    inspector = sa.inspect(op.get_bind())
    if "alembic_version" not in inspector.get_table_names():
        raise RuntimeError("refusing to alter a missing alembic_version table")

    columns = {
        column["name"]: column["type"] for column in inspector.get_columns("alembic_version")
    }
    version_type = columns.get("version_num")
    if not isinstance(version_type, sa.VARCHAR) or version_type.length not in {32, 64}:
        raise RuntimeError(
            "refusing to alter alembic_version.version_num unless it is VARCHAR(32) or VARCHAR(64)"
        )
    return version_type.length


def upgrade() -> None:
    if _version_column_length() == 32:
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(32),
            type_=sa.String(64),
        )


def downgrade() -> None:
    if _version_column_length() == 64:
        bind = op.get_bind()
        has_long_version = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM alembic_version WHERE char_length(version_num) > 32)"
            )
        ).scalar_one()
        if has_long_version:
            raise RuntimeError(
                "refusing to shrink alembic_version.version_num while a value exceeds 32 characters"
            )
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(64),
            type_=sa.String(32),
        )

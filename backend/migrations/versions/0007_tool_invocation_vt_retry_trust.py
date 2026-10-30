"""Persist passive VT retry trust outside canonical tool arguments.

Revision ID: 0007_vt_retry_trust
Revises: 0006_tool_seconds_accounting
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_vt_retry_trust"
down_revision: str | None = "0006_tool_seconds_accounting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PARAMETER = "_vt_passive_retry_adapter_trusted"
_COLUMN = "passive_read_retry_trusted"


def upgrade() -> None:
    op.add_column("tool_invocations", sa.Column(_COLUMN, sa.Boolean(), nullable=True))
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"""
            UPDATE tool_invocations
            SET {_COLUMN} = CASE
                    WHEN jsonb_typeof(normalized_parameters -> :parameter) = 'boolean'
                    THEN (normalized_parameters ->> :parameter)::boolean
                    ELSE false
                END,
                normalized_parameters = normalized_parameters - :parameter
            WHERE normalized_parameters ? :parameter
            """
        ),
        {"parameter": _PARAMETER},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"""
            UPDATE tool_invocations
            SET normalized_parameters = jsonb_set(
                normalized_parameters,
                '{{{_PARAMETER}}}',
                to_jsonb({_COLUMN}),
                true
            )
            WHERE {_COLUMN} IS NOT NULL
              AND NOT (normalized_parameters ? '{_PARAMETER}')
            """
        )
    )
    op.drop_column("tool_invocations", _COLUMN)

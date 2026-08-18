"""Allow the subject_production conversation purpose.

Revision 0016 introduced the `subject_production` purpose in the ORM metadata
but never widened the CHECK constraint that already existed on
`model_conversations`. Every production run therefore died the moment it tried
to open its dedicated conversation, with a check violation instead of a usable
error.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_model_conversations_purpose"
_TABLE = "model_conversations"

_PURPOSES_BEFORE = "'discovery', 'analyst_assistance', 'pivot_research', 'drafting', 'critic'"
_PURPOSES_AFTER = f"{_PURPOSES_BEFORE}, 'subject_production'"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, f"purpose IN ({_PURPOSES_AFTER})")


def downgrade() -> None:
    # Conversations opened by the production workflow would violate the older,
    # narrower constraint, so they go with it.
    op.execute(f"DELETE FROM {_TABLE} WHERE purpose = 'subject_production'")
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, f"purpose IN ({_PURPOSES_BEFORE})")

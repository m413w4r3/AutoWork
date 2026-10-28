"""Treat tool-second reservations as accounting estimates, not spend caps."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from alembic import op
from sqlalchemy import inspect, text

revision: str = "0006_tool_seconds_accounting"
down_revision: str | None = "0005_sample_acquisition_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "investigation_budget_reservation_amounts"
_CONSTRAINT = "ck_budget_reservation_amounts_values"
_OLD_EXPRESSION = (
    "(reserved_amount IS NULL OR reserved_amount >= 0) AND "
    "(committed_amount IS NULL OR committed_amount >= 0) AND "
    "(reserved_amount IS NOT NULL OR committed_amount IS NOT NULL) AND "
    "(committed_amount IS NULL OR committed_amount <= coalesce(reserved_amount, 0))"
)
_NEW_EXPRESSION = (
    "(reserved_amount IS NULL OR reserved_amount >= 0) AND "
    "(committed_amount IS NULL OR committed_amount >= 0) AND "
    "(reserved_amount IS NOT NULL OR committed_amount IS NOT NULL) AND "
    "(dimension = 'tool_seconds' OR committed_amount IS NULL OR "
    "committed_amount <= coalesce(reserved_amount, 0))"
)


def _tokens(value: str) -> tuple[str, ...]:
    normalized = re.sub(r"::(?:text|character varying|bigint|integer)\b", "", value)
    return tuple(
        token.casefold()
        for token in re.findall(
            r"->>|>=|<=|<>|!=|=|\(|\)|,|[a-z_][a-z0-9_]*|\d+|'[^']*'",
            normalized,
            flags=re.IGNORECASE,
        )
        if token not in {"(", ")"}
    )


def _current_expression(bind: Any) -> tuple[str, ...]:
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        raise RuntimeError("tool-seconds migration preflight rejected missing budget table")
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if not {"dimension", "reserved_amount", "committed_amount"} <= columns:
        raise RuntimeError("tool-seconds migration preflight rejected budget table columns")
    checks = {
        item["name"]: _tokens(item["sqltext"]) for item in inspector.get_check_constraints(_TABLE)
    }
    if _CONSTRAINT not in checks:
        raise RuntimeError("tool-seconds migration preflight rejected missing budget check")
    actual = checks[_CONSTRAINT]
    if actual not in {_tokens(_OLD_EXPRESSION), _tokens(_NEW_EXPRESSION)}:
        raise RuntimeError("tool-seconds migration preflight rejected budget check drift")
    return actual


def upgrade() -> None:
    bind = op.get_bind()
    current = _current_expression(bind)
    if current == _tokens(_NEW_EXPRESSION):
        return
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _NEW_EXPRESSION)


def downgrade() -> None:
    bind = op.get_bind()
    current = _current_expression(bind)
    if current == _tokens(_OLD_EXPRESSION):
        return
    overruns = bind.execute(
        text(
            f"SELECT count(*) FROM {_TABLE} "
            "WHERE dimension = 'tool_seconds' "
            "AND committed_amount > coalesce(reserved_amount, 0)"
        )
    ).scalar_one()
    if overruns:
        raise RuntimeError("cannot downgrade while tool-second accounting overruns exist")
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _OLD_EXPRESSION)

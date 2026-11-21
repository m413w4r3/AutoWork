"""Allow evidence-backed bar chart proposal operations.

Revision ID: 0017_editorial_proposal_bar_chart_operation
Revises: 0016_editorial_drafts
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0017_editorial_proposal_bar_chart_operation"
down_revision: str | None = "0016_editorial_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "editorial_proposals"
_CONSTRAINT = "ck_editorial_proposals_operation"
_OLD_OPERATION_VALUES = (
    "'rewrite_title', 'rewrite_lead', 'rewrite_paragraph', 'insert_paragraph', "
    "'delete_paragraph', 'rewrite_section', 'insert_section', 'delete_section', "
    "'merge_sections', 'split_section', 'rename_section', 'create_table', "
    "'rewrite_table', 'delete_table', 'create_diagram', 'rewrite_diagram', "
    "'delete_diagram', 'create_chart', 'rewrite_chart', 'delete_chart', "
    "'create_source_figure', 'rewrite_source_figure', 'replace_source_figure', "
    "'delete_source_figure'"
)
_NEW_OPERATION_VALUES = (
    "'rewrite_title', 'rewrite_lead', 'rewrite_paragraph', 'insert_paragraph', "
    "'delete_paragraph', 'rewrite_section', 'insert_section', 'delete_section', "
    "'merge_sections', 'split_section', 'rename_section', 'create_table', "
    "'create_bar_chart', 'rewrite_table', 'delete_table', 'create_diagram', "
    "'rewrite_diagram', 'delete_diagram', 'create_chart', 'rewrite_chart', "
    "'delete_chart', 'create_source_figure', 'rewrite_source_figure', "
    "'replace_source_figure', 'delete_source_figure'"
)


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    normalized = re.sub(r"::(?:character varying|text)(?:\[\])?", "", normalized)
    normalized = re.sub(
        r"=\s*any\s*\(\s*\(\s*array\s*\[(.*?)\]\s*\)\s*\)",
        r"in (\1)",
        normalized,
    )
    normalized = re.sub(r"\(([a-z_][a-z0-9_.]*)\)", r"\1", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        closes_at_end = True
        for index, character in enumerate(normalized):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(normalized) - 1:
                    closes_at_end = False
                    break
        if not closes_at_end or depth != 0:
            break
        normalized = normalized[1:-1].strip()
    return normalized


def _operation_check_state(bind: Connection) -> str:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        raise RuntimeError(f"{_TABLE}: expected present, actual absent")

    checks = inspector.get_check_constraints(_TABLE)
    operation_checks = [
        check
        for check in checks
        if re.search(r"\boperation\b", str(check.get("sqltext", "")), re.IGNORECASE)
    ]
    if len(operation_checks) != 1 or operation_checks[0].get("name") != _CONSTRAINT:
        names = sorted(str(check.get("name")) for check in operation_checks)
        raise RuntimeError(
            f"{_TABLE} preflight rejected: expected only {_CONSTRAINT}, actual {names}"
        )

    actual_sql = _normalize_sql(str(operation_checks[0].get("sqltext")))
    if actual_sql == _normalize_sql(f"operation IN ({_OLD_OPERATION_VALUES})"):
        return "old"
    if actual_sql == _normalize_sql(f"operation IN ({_NEW_OPERATION_VALUES})"):
        return "new"
    raise RuntimeError(f"{_TABLE} preflight rejected: {_CONSTRAINT} has an unexpected definition")


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError(f"{_TABLE}: expected present, actual absent")
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))

    state = _operation_check_state(bind)
    if state == "new":
        return

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"operation IN ({_NEW_OPERATION_VALUES})",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        raise RuntimeError(f"{_TABLE}: expected present, actual absent")
    bind.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))

    state = _operation_check_state(bind)
    if state != "new":
        raise RuntimeError(
            f"refusing to downgrade {_CONSTRAINT}: expected the expanded operation set"
        )
    row_count = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE operation = 'create_bar_chart'")
    ).scalar_one()
    if row_count:
        raise RuntimeError(
            f"refusing to downgrade {_CONSTRAINT} while create_bar_chart proposals exist"
        )

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        f"operation IN ({_OLD_OPERATION_VALUES})",
    )

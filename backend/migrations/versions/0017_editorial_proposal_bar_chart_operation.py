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

_OLD_OPERATION_SET = frozenset(re.findall(r"'([a-z_]+)'", _OLD_OPERATION_VALUES))
_NEW_OPERATION_SET = frozenset(re.findall(r"'([a-z_]+)'", _NEW_OPERATION_VALUES))

_SQL_OPERATION_VALUE = r"'[a-z_]+'(?:\s*::\s*(?:character varying|text))?"
_SQL_OPERATION_VALUE_LIST = rf"(?P<values>{_SQL_OPERATION_VALUE}(?:\s*,\s*{_SQL_OPERATION_VALUE})*)"
_SQL_OPERATION_COLUMN = r"(?:operation|\(\s*operation\s*\))"
_SQL_OPERATION_CAST = r"(?:\s*::\s*(?:character varying|text))?"
_OPERATION_IN_CHECK = re.compile(
    rf"{_SQL_OPERATION_COLUMN}{_SQL_OPERATION_CAST}\s+IN\s*"
    rf"\(\s*{_SQL_OPERATION_VALUE_LIST}\s*\)",
    re.IGNORECASE,
)
_OPERATION_ARRAY_CHECK = re.compile(
    rf"{_SQL_OPERATION_COLUMN}{_SQL_OPERATION_CAST}\s*=\s*ANY\s*\(\s*"
    rf"\(?\s*ARRAY\s*\[\s*{_SQL_OPERATION_VALUE_LIST}\s*\]\s*\)?"
    rf"\s*(?:::\s*text\s*\[\])?\s*\)",
    re.IGNORECASE,
)


def _strip_outer_parentheses(value: str) -> str:
    normalized = value.strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        in_string = False
        closes_at_end = True
        index = 0
        while index < len(normalized):
            character = normalized[index]
            if character == "'":
                if in_string and index + 1 < len(normalized) and normalized[index + 1] == "'":
                    index += 2
                    continue
                in_string = not in_string
            elif not in_string and character == "(":
                depth += 1
            elif not in_string and character == ")":
                depth -= 1
                if depth < 0:
                    closes_at_end = False
                    break
                if depth == 0 and index != len(normalized) - 1:
                    closes_at_end = False
                    break
            index += 1
        if in_string or depth != 0 or not closes_at_end:
            break
        normalized = normalized[1:-1].strip()
    return normalized


def _parse_operation_check_values(sqltext: str | None) -> list[str] | None:
    if sqltext is None:
        return None
    expression = _strip_outer_parentheses(sqltext)
    for pattern in (_OPERATION_IN_CHECK, _OPERATION_ARRAY_CHECK):
        match = pattern.fullmatch(expression)
        if match is not None:
            return re.findall(r"'([a-z_]+)'", match.group("values"))
    return None


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

    actual_values = _parse_operation_check_values(str(operation_checks[0].get("sqltext", "")))
    if actual_values is None:
        raise RuntimeError(
            f"{_TABLE} preflight rejected: {_CONSTRAINT} has an unexpected definition"
        )
    if len(actual_values) != len(set(actual_values)):
        raise RuntimeError(
            f"{_TABLE} preflight rejected: {_CONSTRAINT} contains duplicate operation values"
        )

    actual_set = set(actual_values)
    if actual_set == _OLD_OPERATION_SET:
        return "old"
    if actual_set == _NEW_OPERATION_SET:
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

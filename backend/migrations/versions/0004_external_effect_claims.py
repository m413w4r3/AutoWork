"""Add durable one-request external effect claims and use lineage.

The revision intentionally follows 0003_osint_open_policy. That parent is
provided by the adjacent OSINT policy lot and is not part of this change.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

from cti_app.infrastructure.database.models.external_effects import (
    ExternalEffectClaimRow,
    ExternalEffectUseRow,
)

revision: str = "0004_external_effect_claims"
down_revision: str | None = "0003_osint_open_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("external_effect_claim", "external_effect_use")


def _tokens(value: str) -> tuple[str, ...]:
    normalized = re.sub(
        r"::(?:text|character varying|jsonb|uuid|bigint|integer)\b",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return tuple(
        token.casefold()
        for token in re.findall(
            r"->>|>=|<=|<>|!=|=|~|\(|\)|,|[a-z_][a-z0-9_]*|\d+|'[^']*'",
            normalized,
        )
        if token not in {"(", ")"}
    )


def _schema_objects(bind: Any) -> set[str]:
    rows = bind.execute(
        text(
            """
            SELECT c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = ANY(:names)
            UNION ALL
            SELECT con.conname
            FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace
            WHERE n.nspname = current_schema()
              AND con.conname LIKE '%external_effect_%'
            UNION ALL
            SELECT c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname LIKE 'ix_external_effect_%'
            """
        ),
        {"names": list(_TABLES)},
    ).scalars()
    return set(rows)


def _expected_tables() -> tuple[Any, Any]:
    return ExternalEffectClaimRow.__table__, ExternalEffectUseRow.__table__


def _assert_table_shape(bind: Any, table: Any) -> None:
    inspector = inspect(bind)
    name = table.name
    actual_columns = inspector.get_columns(name)
    expected_columns = {column.name: column for column in table.columns}
    actual_by_name = {column["name"]: column for column in actual_columns}
    if set(actual_by_name) != set(expected_columns):
        raise RuntimeError(f"external effect migration preflight rejected column drift in {name}")
    dialect = postgresql.dialect()
    for column_name, column in expected_columns.items():
        actual = actual_by_name[column_name]
        expected_type = column.type.compile(dialect=dialect).upper()
        actual_type = str(actual["type"]).upper()
        if expected_type != actual_type or bool(actual["nullable"]) != bool(column.nullable):
            raise RuntimeError(
                f"external effect migration preflight rejected type/nullability drift "
                f"in {name}.{column_name}"
            )
        expected_default = (
            _tokens(str(column.server_default.arg)) if column.server_default is not None else None
        )
        actual_default = (
            _tokens(str(actual["default"])) if actual.get("default") is not None else None
        )
        if actual_default != expected_default:
            raise RuntimeError(
                f"external effect migration preflight rejected default drift in "
                f"{name}.{column_name}"
            )

    expected_constraints: dict[str, tuple[str, tuple[str, ...], Any]] = {}
    for constraint in table.constraints:
        if constraint.name is None:
            raise RuntimeError(f"external effect schema has an unnamed constraint in {name}")
        local_columns = tuple(column.name for column in constraint.columns)
        if constraint.__class__.__name__ == "PrimaryKeyConstraint":
            expected_constraints[constraint.name] = ("p", local_columns, None)
        elif constraint.__class__.__name__ == "UniqueConstraint":
            expected_constraints[constraint.name] = ("u", local_columns, None)
        elif constraint.__class__.__name__ == "CheckConstraint":
            expected_constraints[constraint.name] = (
                "c",
                local_columns,
                _tokens(str(constraint.sqltext)),
            )
        elif constraint.__class__.__name__ == "ForeignKeyConstraint":
            elements = tuple(constraint.elements)
            target_table = elements[0].column.table.name
            target_columns = tuple(element.column.name for element in elements)
            ondelete = constraint.ondelete
            expected_constraints[constraint.name] = (
                "f",
                local_columns,
                (target_table, target_columns, ondelete),
            )
        else:
            raise RuntimeError(f"unsupported external effect constraint {constraint!r}")

    actual_constraints: dict[str, tuple[str, tuple[str, ...], Any]] = {}
    primary = inspector.get_pk_constraint(name)
    if primary.get("name"):
        actual_constraints[primary["name"]] = (
            "p",
            tuple(primary.get("constrained_columns") or ()),
            None,
        )
    for item in inspector.get_unique_constraints(name):
        actual_constraints[item["name"]] = ("u", tuple(item.get("column_names") or ()), None)
    for item in inspector.get_check_constraints(name):
        actual_constraints[item["name"]] = ("c", (), _tokens(item["sqltext"]))
    for item in inspector.get_foreign_keys(name):
        options = item.get("options") or {}
        actual_constraints[item["name"]] = (
            "f",
            tuple(item.get("constrained_columns") or ()),
            (
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
                (options.get("ondelete") or "").upper() or None,
            ),
        )
    if set(actual_constraints) != set(expected_constraints):
        raise RuntimeError(
            f"external effect migration preflight rejected constraint drift in {name}"
        )
    for constraint_name, (kind, local_columns, detail) in expected_constraints.items():
        actual = actual_constraints[constraint_name]
        if kind == "c":
            matches = actual[0] == kind and actual[2] == detail
        elif kind == "f":
            expected_fk = (detail[0], detail[1], (detail[2] or "").upper() or None)
            matches = actual[0] == kind and actual[1] == local_columns and actual[2] == expected_fk
        else:
            matches = actual[0] == kind and actual[1] == local_columns
        if not matches:
            raise RuntimeError(
                f"external effect migration preflight rejected constraint drift: {constraint_name}"
            )

    expected_indexes = {
        index.name: (tuple(column.name for column in index.columns), bool(index.unique))
        for index in table.indexes
    }
    actual_indexes = {}
    for item in inspector.get_indexes(name):
        # PostgreSQL also reports indexes backing PK/UNIQUE constraints.
        if item.get("duplicates_constraint") is not None:
            continue
        actual_indexes[item["name"]] = (
            tuple(item.get("column_names") or ()),
            bool(item.get("unique")),
        )
    if actual_indexes != expected_indexes:
        raise RuntimeError(f"external effect migration preflight rejected index drift in {name}")


def _preflight(bind: Any) -> bool:
    present = _schema_objects(bind)
    expected_names = set(_TABLES)
    related = {name for name in present if "external_effect_" in name}
    if not related:
        return False
    if not expected_names.issubset(related):
        raise RuntimeError(
            "external effect migration preflight rejected partial or conflicting objects: "
            + ", ".join(sorted(related))
        )
    inspector = inspect(bind)
    actual_tables = set(inspector.get_table_names())
    if not expected_names.issubset(actual_tables):
        raise RuntimeError(
            "external effect migration preflight rejected a non-table name collision"
        )
    for table in _expected_tables():
        _assert_table_shape(bind, table)
    return True


def upgrade() -> None:
    bind = op.get_bind()
    if _preflight(bind):
        return
    claim, use = _expected_tables()
    claim.create(bind=bind, checkfirst=False)
    use.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text("LOCK TABLE external_effect_use, external_effect_claim IN ACCESS EXCLUSIVE MODE")
    )
    rows_exist = bind.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM external_effect_claim) "
            "OR EXISTS (SELECT 1 FROM external_effect_use)"
        )
    ).scalar_one()
    if rows_exist:
        raise RuntimeError(
            "refusing to drop external effect claims or use lineage while rows exist"
        )
    use, claim = reversed(_expected_tables())
    use.drop(bind=bind, checkfirst=False)
    claim.drop(bind=bind, checkfirst=False)

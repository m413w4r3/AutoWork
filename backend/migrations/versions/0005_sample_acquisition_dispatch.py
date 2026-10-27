"""Add durable pre-send dispatch identity for VT sample acquisition.

The historical acquisition ledger remains append-only and post-transport. This
revision adds a separate logical dispatch row and a one-to-one claim link so a
physical request can be authorized before its first byte without rewriting
existing claim history.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from alembic import op
from sqlalchemy import Column, Uuid, inspect, text
from sqlalchemy.dialects import postgresql

from cti_app.infrastructure.database.models.external_effects import (
    ExternalEffectAcquisitionDispatchRow,
)
from cti_app.infrastructure.database.models.production import SampleAcquisitionDispatchAttemptRow

revision: str = "0005_sample_acquisition_dispatch"
down_revision: str | None = "0004_external_effect_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISPATCH = "sample_acquisition_dispatch_attempts"
_LINK = "effect_acquisition_dispatch_links"
_TERMINAL = "sample_acquisition_attempts"
_INDEX = "uq_sample_acquisition_attempt_dispatch"
_FK = "fk_sample_acquisition_attempts_dispatch"


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


def _assert_table_shape(bind: Any, table: Any) -> None:
    inspector = inspect(bind)
    actual_columns = {column["name"]: column for column in inspector.get_columns(table.name)}
    expected_columns = {column.name: column for column in table.columns}
    if set(actual_columns) != set(expected_columns):
        raise RuntimeError(f"sample acquisition dispatch preflight rejected {table.name} columns")
    dialect = postgresql.dialect()
    for name, column in expected_columns.items():
        actual = actual_columns[name]
        expected_default = (
            _tokens(str(column.server_default.arg)) if column.server_default is not None else None
        )
        actual_default = (
            _tokens(str(actual["default"])) if actual.get("default") is not None else None
        )
        if (
            column.type.compile(dialect=dialect).upper() != str(actual["type"]).upper()
            or bool(column.nullable) != bool(actual["nullable"])
            or expected_default != actual_default
        ):
            raise RuntimeError(
                f"sample acquisition dispatch preflight rejected {table.name}.{name} drift"
            )

    expected_constraints: dict[str, tuple[str, tuple[str, ...], Any]] = {}
    for constraint in table.constraints:
        if constraint.name is None:
            raise RuntimeError(
                f"sample acquisition dispatch schema has unnamed constraint in {table.name}"
            )
        columns = tuple(column.name for column in constraint.columns)
        kind = constraint.__class__.__name__
        if kind == "PrimaryKeyConstraint":
            expected_constraints[constraint.name] = ("p", columns, None)
        elif kind == "UniqueConstraint":
            expected_constraints[constraint.name] = ("u", columns, None)
        elif kind == "CheckConstraint":
            expected_constraints[constraint.name] = ("c", (), _tokens(str(constraint.sqltext)))
        elif kind == "ForeignKeyConstraint":
            elements = tuple(constraint.elements)
            expected_constraints[constraint.name] = (
                "f",
                columns,
                (
                    elements[0].column.table.name,
                    tuple(element.column.name for element in elements),
                    (constraint.ondelete or "").upper() or None,
                ),
            )
        else:
            raise RuntimeError(f"sample acquisition dispatch preflight found unsupported {kind}")

    actual_constraints: dict[str, tuple[str, tuple[str, ...], Any]] = {}
    primary = inspector.get_pk_constraint(table.name)
    if primary.get("name"):
        actual_constraints[primary["name"]] = (
            "p",
            tuple(primary.get("constrained_columns") or ()),
            None,
        )
    for item in inspector.get_unique_constraints(table.name):
        actual_constraints[item["name"]] = ("u", tuple(item.get("column_names") or ()), None)
    for item in inspector.get_check_constraints(table.name):
        actual_constraints[item["name"]] = ("c", (), _tokens(item["sqltext"]))
    for item in inspector.get_foreign_keys(table.name):
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
    if actual_constraints != expected_constraints:
        raise RuntimeError(
            f"sample acquisition dispatch preflight rejected {table.name} constraints"
        )

    expected_indexes = {
        item.name: (tuple(column.name for column in item.columns), bool(item.unique))
        for item in table.indexes
    }
    actual_indexes = {
        item["name"]: (tuple(item.get("column_names") or ()), bool(item.get("unique")))
        for item in inspector.get_indexes(table.name)
        if item.get("duplicates_constraint") is None
    }
    if actual_indexes != expected_indexes:
        raise RuntimeError(f"sample acquisition dispatch preflight rejected {table.name} indexes")


def _preflight(bind: Any) -> bool:
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    terminal_columns = {column["name"] for column in inspector.get_columns(_TERMINAL)}
    indexes = {index["name"] for index in inspector.get_indexes(_TERMINAL)}
    foreign_keys = {item["name"] for item in inspector.get_foreign_keys(_TERMINAL)}
    dispatch_exists = _DISPATCH in tables
    link_exists = _LINK in tables
    column_exists = "dispatch_attempt_id" in terminal_columns
    index_exists = _INDEX in indexes
    fk_exists = _FK in foreign_keys
    present = (dispatch_exists, link_exists, column_exists, index_exists, fk_exists)
    if not any(present):
        return False
    if not all(present):
        raise RuntimeError("sample acquisition dispatch preflight rejected partial schema")

    expected_dispatch = SampleAcquisitionDispatchAttemptRow.__table__
    expected_link = ExternalEffectAcquisitionDispatchRow.__table__
    for table in (expected_dispatch, expected_link):
        _assert_table_shape(bind, table)

    terminal_column = next(
        column
        for column in inspector.get_columns(_TERMINAL)
        if column["name"] == "dispatch_attempt_id"
    )
    if str(terminal_column["type"]).upper() != "UUID" or terminal_column["nullable"] is not True:
        raise RuntimeError("sample acquisition dispatch preflight rejected terminal column drift")
    terminal_fk = next(
        item for item in inspector.get_foreign_keys(_TERMINAL) if item["name"] == _FK
    )
    if (
        terminal_fk.get("constrained_columns") != ["dispatch_attempt_id"]
        or terminal_fk.get("referred_table") != _DISPATCH
        or terminal_fk.get("referred_columns") != ["id"]
        or (terminal_fk.get("options") or {}).get("ondelete", "").upper() != "RESTRICT"
    ):
        raise RuntimeError("sample acquisition dispatch preflight rejected terminal FK drift")
    terminal_index = next(
        item for item in inspector.get_indexes(_TERMINAL) if item["name"] == _INDEX
    )
    terminal_predicate = (terminal_index.get("dialect_options") or {}).get("postgresql_where")
    if (
        terminal_index.get("column_names") != ["dispatch_attempt_id"]
        or not terminal_index.get("unique")
        or not isinstance(terminal_predicate, str)
        or _tokens(terminal_predicate) != _tokens("dispatch_attempt_id IS NOT NULL")
    ):
        raise RuntimeError("sample acquisition dispatch preflight rejected terminal index drift")
    return True


def upgrade() -> None:
    bind = op.get_bind()
    if _preflight(bind):
        return
    SampleAcquisitionDispatchAttemptRow.__table__.create(bind=bind, checkfirst=False)
    op.add_column(_TERMINAL, Column("dispatch_attempt_id", Uuid(), nullable=True))
    op.create_foreign_key(
        _FK, _TERMINAL, _DISPATCH, ["dispatch_attempt_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(
        _INDEX,
        _TERMINAL,
        ["dispatch_attempt_id"],
        unique=True,
        postgresql_where=text("dispatch_attempt_id IS NOT NULL"),
    )
    ExternalEffectAcquisitionDispatchRow.__table__.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            "LOCK TABLE external_effect_claim, effect_acquisition_dispatch_links, "
            "sample_acquisition_attempts, sample_acquisition_dispatch_attempts "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    rows_exist = bind.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM effect_acquisition_dispatch_links) "
            "OR EXISTS (SELECT 1 FROM sample_acquisition_dispatch_attempts) "
            "OR EXISTS (SELECT 1 FROM sample_acquisition_attempts "
            "WHERE dispatch_attempt_id IS NOT NULL)"
        )
    ).scalar_one()
    if rows_exist:
        raise RuntimeError("refusing to drop sample acquisition dispatch lineage while rows exist")
    ExternalEffectAcquisitionDispatchRow.__table__.drop(bind=bind, checkfirst=False)
    op.drop_index(_INDEX, table_name=_TERMINAL)
    op.drop_constraint(_FK, _TERMINAL, type_="foreignkey")
    op.drop_column(_TERMINAL, "dispatch_attempt_id")
    SampleAcquisitionDispatchAttemptRow.__table__.drop(bind=bind, checkfirst=False)

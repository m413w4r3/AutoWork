"""Static oracles for migration 0019 and the canonical workspace rows.

The live PostgreSQL oracles live in ``tests/integration``; these tests pin the
frozen metadata contract without a database: revision chain, per-file revision
identity, metadata-only columns and ORM/migration concordance.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
from types import ModuleType

import sqlalchemy as sa

from cti_app.application.workspace_agent import workspace_revision_id
from cti_app.infrastructure.database.models import (  # noqa: F401
    workspace as workspace_models,
)
from cti_app.infrastructure.database.models.base import Base

MIGRATION: ModuleType = importlib.import_module("migrations.versions.0019_workspace_bindings")

WORKSPACE_TABLES = (
    "workspace_bindings",
    "workspace_file_revisions",
    "workspace_file_events",
    "workspace_sync_cursors",
)


def _normalize_default(value: object) -> str | None:
    if value is None:
        return None
    argument = getattr(value, "arg", value)
    return str(argument).replace("::character varying", "")


def test_workspace_revision_chain_follows_0018() -> None:
    assert MIGRATION.revision == "0019_workspace_bindings"
    assert MIGRATION.down_revision == "0018_editorial_enrichment_element_operations"


def test_workspace_revisions_unique_per_file() -> None:
    table = Base.metadata.tables["workspace_file_revisions"]
    assert {column.name for column in table.primary_key.columns} == {
        "workspace_id",
        "revision_id",
    }
    unique_column_sets = {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert ("path", "revision_id", "workspace_id") in unique_column_sets
    assert not {
        "content",
        "data",
        "bytes",
        "blob_id",
        "content_blob_id",
        "body",
    } & set(table.columns)


def test_orm_tables_match_the_migration_declaration() -> None:
    expected = MIGRATION._expected_tables()
    assert set(expected) == set(WORKSPACE_TABLES)
    for name in WORKSPACE_TABLES:
        orm_table = Base.metadata.tables[name]
        migration_table = expected[name]
        assert [column.name for column in orm_table.columns] == [
            column.name for column in migration_table.columns
        ], name
        for orm_column, migration_column in zip(
            orm_table.columns, migration_table.columns, strict=True
        ):
            assert str(orm_column.type) == str(migration_column.type), (
                name,
                orm_column.name,
            )
            assert orm_column.nullable == migration_column.nullable, (
                name,
                orm_column.name,
            )
            assert _normalize_default(orm_column.server_default) == _normalize_default(
                migration_column.server_default
            ), (name, orm_column.name)
        assert {
            constraint.name
            for constraint in orm_table.constraints
            if not isinstance(constraint, sa.PrimaryKeyConstraint)
        } == {
            constraint.name
            for constraint in migration_table.constraints
            if not isinstance(constraint, sa.PrimaryKeyConstraint)
        }, name
        assert {column.name for column in orm_table.primary_key.columns} == {
            column.name for column in migration_table.primary_key.columns
        }, name
        assert {index.name for index in orm_table.indexes} == {
            index.name for index in migration_table.indexes
        }, name


def test_workspace_file_events_keep_received_until_applied() -> None:
    table = Base.metadata.tables["workspace_file_events"]
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert "received" in checks["ck_workspace_file_events_import_status"]
    assert "applied" in checks["ck_workspace_file_events_import_status"]
    assert table.c.import_status.server_default is not None
    assert "imported_at" in table.c
    assert table.c.imported_at.nullable is True


def test_workspace_sync_cursor_is_one_row_per_workspace() -> None:
    table = Base.metadata.tables["workspace_sync_cursors"]
    assert {column.name for column in table.primary_key.columns} == {"workspace_id"}
    assert [column.name for column in table.columns] == [
        "workspace_id",
        "generation",
        "cursor",
        "last_scan_at",
    ]


def test_workspace_binding_pins_the_manifest_identity() -> None:
    table = Base.metadata.tables["workspace_bindings"]
    assert {
        "workspace_id",
        "investigation_id",
        "subject_id",
        "delivery_code",
        "article_slug",
        "synthesis_sha256",
        "created_at",
    } == set(table.columns.keys())
    assert not {"host_path", "path", "root"} & set(table.columns)


def test_workspace_revision_id_is_deterministic_and_per_file() -> None:
    first = workspace_revision_id(
        workspace_id="ws-" + "1" * 32, path="notes/humaines/a.md", sha256="a" * 64
    )
    same = workspace_revision_id(
        workspace_id="ws-" + "1" * 32, path="notes/humaines/a.md", sha256="a" * 64
    )
    other_path = workspace_revision_id(
        workspace_id="ws-" + "1" * 32, path="notes/humaines/b.md", sha256="a" * 64
    )
    manual = (
        "rev-"
        + hashlib.sha256(
            ("ws-" + "1" * 32).encode("utf-8")
            + b"\x00"
            + b"notes/humaines/a.md"
            + b"\x00"
            + ("a" * 64).encode("ascii")
        ).hexdigest()[:32]
    )
    assert first == same == manual
    assert first != other_path
    assert first.startswith("rev-")
    assert len(first) == 36


def test_downgrade_guard_refuses_used_workspace_tables() -> None:
    source = inspect.getsource(MIGRATION.downgrade)
    assert set(MIGRATION._TABLE_ORDER) == set(WORKSPACE_TABLES)
    assert "refusing to downgrade" in source
    assert "SELECT count(*)" in source

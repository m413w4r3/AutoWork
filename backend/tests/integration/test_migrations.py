"""Exhaustive coverage of the Alembic migration chain.

This module validates, against a real (temporary) PostgreSQL database:

1. the exact set of application tables produced by ``alembic upgrade head``;
2. that this set matches ``Base.metadata.tables`` (module-level ORM models),
   modulo a short, explicitly documented list of known exceptions;
3. for every ORM-mapped table: columns (name, type family, nullability,
   varchar length), primary key, foreign keys (incl. ``ON DELETE``), unique
   constraints, check constraints and indexes (incl. partial indexes) —
   comparing the live, migrated schema against the SQLAlchemy ``Table``
   objects structurally, rather than hand-duplicating every definition;
4. the exact set of custom PostgreSQL functions and triggers the migrations
   install, and which function each trigger is bound to;
5. the *behaviour* of the two guard families (TLP-downgrade prevention and
   append-only enforcement), not just their catalog presence;
6. a full ``upgrade head`` -> ``downgrade base`` -> ``upgrade head`` cycle,
   asserting tables/functions/triggers are completely gone after the full
   downgrade and fully restored after re-upgrading;
7. [R07a] that the single squashed ``0001_baseline`` migration reproduces,
   byte-for-byte, the exact catalog manifest (tables, columns, constraints,
   indexes, sequences, functions, triggers) captured from the former
   23-migration chain (0001..0023) before it was squashed. This is a
   structural manifest comparison, not a sorted ``pg_dump`` diff.

Structural comparisons are generic (driven by ``Base.metadata``) rather than
transcribed by hand, so the exhaustive checks stay correct as new migrations
and models are added instead of silently drifting out of date.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql.schema import Table

from cti_app.infrastructure.database.models import Base
from tests.integration.conftest import _alembic_config
from tests.integration.schema_catalog import capture_schema_catalog

# [R07a] Canonical structural manifest captured from the former 23-migration
# chain (0001..0023) on a fresh database, before it was squashed into the
# single `0001_baseline` migration. See `schema_catalog.py` for the exact
# shape and `migrations/versions/0001_baseline.py` for the squash itself.
_R07A_PRE_SQUASH_CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "schema" / "r07a_pre_squash_catalog.json"
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Expected shape of the migrated database
# ---------------------------------------------------------------------------

# Tables created directly by migrations 0021/0022 that were never wired into
# an ORM model in `cti_app.infrastructure.database.models`. They are real,
# intentional parts of the migrated schema (editorial preservation / replay
# comparison support), just not yet read/written through the SQLAlchemy layer.
UNMAPPED_TABLES = frozenset(
    {
        "brief_amendments",  # 0021_editorial_preservation_increment_3
        "editorial_update_decisions",  # 0021_editorial_preservation_increment_3
        "replay_identity_mappings",  # 0022_replay_identity_mapping_increment_4
        "replay_comparisons",  # 0022_replay_identity_mapping_increment_4
    }
)

ALEMBIC_TABLE = "alembic_version"

EXPECTED_TABLES = frozenset(Base.metadata.tables) | UNMAPPED_TABLES | {ALEMBIC_TABLE}

# --- Known, pre-existing ORM/database drift --------------------------------
#
# The comparisons below are written to be exact (`==`), not subset checks, so
# that any *new* drift between a migration and `models.py` fails the suite.
# The migration chain has nonetheless accumulated a number of small, real
# discrepancies over time (typically: a migration was hand-written straight
# against the database and `models.py` was never updated to match, or vice
# versa). Each one is catalogued here instead of silently loosening the
# corresponding assertion, so the exhaustive checks stay exact everywhere else
# and every known gap is visible, attributed to the migration that caused it,
# and easy to grep for when someone eventually reconciles them.

# (local columns, referred table, referred columns, ON DELETE) — shared by
# both the ORM-derived and the reflected-from-database foreign key shapes.
ForeignKeyShape = tuple[tuple[str, ...], str, tuple[str, ...], str | None]

# Foreign keys present in the database but not declared on the ORM model.
KNOWN_EXTRA_FOREIGN_KEYS: dict[str, set[ForeignKeyShape]] = {
    "brief_evidence_packs": {
        # 0021 added both with raw `op.create_foreign_key`; `BriefEvidencePackRow`
        # declares `built_from_snapshot_id` / `base_pack_id` as bare `Uuid` columns.
        (("built_from_snapshot_id",), "discovery_snapshots", ("id",), "RESTRICT"),
        (("base_pack_id",), "brief_evidence_packs", ("id",), "RESTRICT"),
    },
    "discovery_merge_runs": {
        # 0019 adds this FK by ALTER after creating `discovery_snapshots` (which
        # itself depends on `discovery_merge_runs`); `DiscoveryMergeRunRow`
        # declares `parent_snapshot_id` as a bare `Uuid` column.
        (("parent_snapshot_id",), "discovery_snapshots", ("id",), "RESTRICT"),
    },
}

# Column definitions where the live migrated schema disagrees with the ORM
# model: type family, nullability, or both.
ColumnShape = tuple[str, bool, int | None]
KNOWN_COLUMN_DRIFT: dict[str, dict[str, ColumnShape]] = {
    "brief_evidence_packs": {
        # 0021 added both as nullable; the model declares non-null `JSONB` for
        # `covered_contribution_ids` (actual DB type is `uuid[]`) and non-null
        # for `scope`.
        "covered_contribution_ids": ("array<uuid>", True, None),
        "scope": ("string", True, 10),
    },
    "model_conversation_turns": {
        # 0011 declares `sequence` as `Integer`; the model declares `BigInteger`.
        "sequence": ("integer", False, None),
    },
    "model_conversations": {
        # 0011 declares both as `Integer`; the model declares `BigInteger`.
        "turn_count": ("integer", False, None),
        "version": ("integer", False, None),
    },
    "production_artifacts": {
        # 0016 declares `metadata` as plain `sa.JSON()`; the model maps the
        # `artifact_metadata` attribute to that column as `JSONB`.
        "metadata": ("json", False, None),
    },
    "subject_production_runs": {
        # 0016 declares `error_details` as plain `sa.JSON()`; the model
        # declares `JSONB`.
        "error_details": ("json", True, None),
    },
}

# Check constraints that exist in the database but have no corresponding
# `CheckConstraint` at all on the ORM model (the model's `__table_args__`
# simply never picked them up).
KNOWN_EXTRA_CHECK_CONSTRAINTS: dict[str, set[str]] = {
    "claims": {"ck_claims_local_span"},  # 0008
    "collection_attempts": {  # 0008
        "ck_collection_attempts_decoded_sha256",
        "ck_collection_attempts_encoded_sha256",
    },
    "collection_policy_snapshots": {"ck_collection_policy_snapshots_id"},  # 0008
    "discovery_batches": {"ck_discovery_payload_object"},  # 0005
    "editions": {"ck_editions_complete_month", "ck_editions_languages"},  # 0003
    "editorial_groups": {"ck_editorial_payload_object"},  # 0006
    "human_decisions": {  # 0006
        "ck_human_decisions_groups_array",
        "ck_human_decisions_payload_object",
    },
    "model_conversation_turns": {"ck_model_conversation_turns_hashes"},  # 0011
    "model_output_rejections": {"ck_model_output_rejections_hash"},  # 0012
    "model_runs": {  # 0004 / 0012
        "ck_model_runs_parameters_object",
        "ck_model_runs_output_diagnostic_counts",
        "ck_model_runs_output_references_array",
    },
    "subject_merge_events": {"ck_subject_merge_events_distinct"},  # 0019
}

# Check constraints the model names explicitly, but whose migration
# (0016/0017) created them unnamed, so Postgres fell back to its own
# `<table>_<column>_check` naming convention instead. `{model_name: db_name}`.
KNOWN_CHECK_CONSTRAINT_RENAMES: dict[str, dict[str, str]] = {
    "edition_production_batch_items": {
        "ck_batch_item_position": "edition_production_batch_items_position_check",
    },
    "edition_production_batches": {
        "ck_batch_profile": "edition_production_batches_profile_check",
        "ck_batch_status": "edition_production_batches_status_check",
    },
    "production_artifacts": {
        "ck_artifact_status": "production_artifacts_status_check",
        "ck_artifact_stage": "production_artifacts_stage_check",
        "ck_artifact_version": "production_artifacts_version_check",
        "ck_artifact_input_hash": "production_artifacts_input_hash_check",
    },
    "subject_production_runs": {
        "ck_run_version": "subject_production_runs_version_check",
        "ck_run_status": "subject_production_runs_status_check",
        "ck_run_stage": "subject_production_runs_current_stage_check",
        "ck_run_profile": "subject_production_runs_profile_check",
        "ck_run_number": "subject_production_runs_run_number_check",
    },
}

# (index name, ordered columns, unique, is_partial) — shared by both the
# ORM-derived and the reflected-from-database index shapes.
IndexShape = tuple[str, tuple[str, ...], bool, bool]

# Indexes that exist in the database but have no corresponding `Index` at all
# declared on the ORM model's `__table_args__`.
KNOWN_EXTRA_INDEXES: dict[str, set[IndexShape]] = {
    "conversation_lifecycles": {  # 0023 creates these; ConversationLifecycleRow
        # declares no `Index` objects at all.
        ("ix_conversation_lifecycles_conversation_id", ("conversation_id",), False, False),
        ("ix_conversation_lifecycles_status", ("status",), False, False),
        ("ix_conversation_lifecycles_created_at", ("created_at",), False, False),
    },
    "editorial_groups": {
        # 0019 adds this index alongside `discovery_subject_id`; EditorialGroupRow
        # never declares it.
        ("ix_editorial_groups_discovery_subject", ("discovery_subject_id",), False, False),
    },
}

# (table, trigger_name) -> function_name, for every trigger installed by the
# migration chain. Built by hand from the CREATE TRIGGER / CREATE FUNCTION
# statements across 0001-0023; nothing in this chain drops a trigger or
# function on the way to head, so this is the complete set at "head".
EXPECTED_TRIGGERS: dict[tuple[str, str], str] = {
    ("subjects", "trg_subjects_prevent_tlp_downgrade"): "prevent_tlp_downgrade",
    ("source_documents", "trg_source_documents_prevent_tlp_downgrade"): "prevent_tlp_downgrade",
    ("samples", "trg_samples_prevent_tlp_downgrade"): "prevent_tlp_downgrade",
    ("editions", "trg_editions_prevent_tlp_downgrade"): "prevent_tlp_downgrade",
    ("provenance_events", "trg_provenance_events_append_only"): "reject_provenance_mutation",
    ("edition_audit_events", "trg_edition_audit_events_append_only"): "reject_audit_mutation",
    ("job_events", "trg_job_events_append_only"): "reject_audit_mutation",
    ("human_decisions", "trg_human_decisions_append_only"): "reject_human_decision_mutation",
    ("collection_attempts", "trg_collection_attempts_append_only"): "reject_evidence_mutation",
    ("derived_artifacts", "trg_derived_artifacts_append_only"): "reject_evidence_mutation",
    ("claims", "trg_claims_append_only"): "reject_evidence_mutation",
    ("indicators", "trg_indicators_append_only"): "reject_evidence_mutation",
    (
        "collection_policy_snapshots",
        "trg_collection_policy_snapshots_append_only",
    ): "reject_evidence_mutation",
    (
        "rejected_model_proposals",
        "trg_rejected_model_proposals_append_only",
    ): "reject_evidence_mutation",
    ("brief_evidence_packs", "trg_brief_evidence_packs_append_only"): "reject_evidence_mutation",
    ("brief_drafts", "trg_brief_drafts_append_only"): "reject_evidence_mutation",
    (
        "discovery_intakes",
        "trg_discovery_intakes_append_only",
    ): "reject_discovery_intakes_mutation",
    (
        "subject_merge_events",
        "trg_subject_merge_events_append_only",
    ): "reject_subject_merge_events_mutation",
    (
        "subject_contributions",
        "trg_subject_contributions_append_only",
    ): "reject_subject_contributions_mutation",
}
EXPECTED_FUNCTIONS = frozenset(EXPECTED_TRIGGERS.values())


# ---------------------------------------------------------------------------
# Structural comparison helpers (ORM model  <->  live database)
# ---------------------------------------------------------------------------


def _type_category(sa_type: Any) -> str:
    """Collapse a SQLAlchemy type into a coarse, dialect-independent family.

    Applied identically to `Table.c[...].type` (ORM side) and to
    `inspector.get_columns(...)[...]["type"]` (reflected DB side) so the two
    can be compared for equality without caring whether e.g. a big integer is
    spelled `BigInteger` or reflected as `BIGINT`.
    """
    if isinstance(sa_type, ARRAY):
        return f"array<{_type_category(sa_type.item_type)}>"
    if isinstance(sa_type, JSONB):
        return "jsonb"
    if isinstance(sa_type, Uuid):
        return "uuid"
    if isinstance(sa_type, DateTime):
        return "datetime_tz" if sa_type.timezone else "datetime"
    if isinstance(sa_type, Date):
        return "date"
    if isinstance(sa_type, Boolean):
        return "boolean"
    if isinstance(sa_type, BigInteger):
        return "bigint"
    if isinstance(sa_type, Integer):
        return "integer"
    if isinstance(sa_type, Float):
        return "float"
    if isinstance(sa_type, Text):
        return "text"
    if isinstance(sa_type, String):
        return "string"
    return type(sa_type).__name__.lower()


def _column_shape(sa_type: Any, nullable: bool) -> ColumnShape:
    category = _type_category(sa_type)
    length = getattr(sa_type, "length", None) if category == "string" else None
    return (category, nullable, length)


def _expected_columns(table: Table) -> dict[str, ColumnShape]:
    return {col.name: _column_shape(col.type, bool(col.nullable)) for col in table.columns}


def _expected_primary_key(table: Table) -> frozenset[str]:
    return frozenset(col.name for col in table.primary_key.columns)


def _expected_foreign_keys(table: Table) -> set[ForeignKeyShape]:
    shapes: set[ForeignKeyShape] = set()
    for fkc in table.foreign_key_constraints:
        elements = list(fkc.elements)
        referred_table = elements[0].column.table.name
        shapes.add(
            (
                tuple(fkc.column_keys),
                referred_table,
                tuple(elem.column.name for elem in elements),
                fkc.ondelete,
            )
        )
    return shapes


def _expected_unique_constraints(table: Table) -> set[frozenset[str]]:
    shapes = {
        frozenset(col.name for col in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    shapes.update(frozenset({col.name}) for col in table.columns if col.unique)
    return shapes


def _expected_check_constraint_names(table: Table) -> set[str]:
    return {
        str(constraint.name)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }


def _expected_indexes(table: Table) -> set[IndexShape]:
    shapes: set[IndexShape] = set()
    for idx in table.indexes:
        pg_options: Any = idx.dialect_options.get("postgresql", {})
        is_partial = bool(pg_options.get("where") is not None)
        shapes.add((str(idx.name), tuple(col.name for col in idx.columns), idx.unique, is_partial))
    return shapes


def _actual_columns(inspector: Any, table_name: str) -> dict[str, ColumnShape]:
    return {
        col["name"]: _column_shape(col["type"], col["nullable"])
        for col in inspector.get_columns(table_name)
    }


def _actual_primary_key(inspector: Any, table_name: str) -> frozenset[str]:
    return frozenset(inspector.get_pk_constraint(table_name)["constrained_columns"])


def _actual_foreign_keys(inspector: Any, table_name: str) -> set[ForeignKeyShape]:
    return {
        (
            tuple(fk["constrained_columns"]),
            fk["referred_table"],
            tuple(fk["referred_columns"]),
            fk.get("options", {}).get("ondelete"),
        )
        for fk in inspector.get_foreign_keys(table_name)
    }


def _actual_unique_constraints(inspector: Any, table_name: str) -> set[frozenset[str]]:
    return {frozenset(uc["column_names"]) for uc in inspector.get_unique_constraints(table_name)}


def _actual_check_constraint_names(inspector: Any, table_name: str) -> set[str]:
    return {cc["name"] for cc in inspector.get_check_constraints(table_name)}


def _actual_indexes(inspector: Any, table_name: str) -> set[IndexShape]:
    shapes: set[IndexShape] = set()
    for idx in inspector.get_indexes(table_name):
        if idx.get("duplicates_constraint"):
            # Postgres backs every UNIQUE constraint with an index; that index
            # is already covered by `_expected_unique_constraints`, so it must
            # be excluded here or every unique constraint would be demanded
            # twice: once as a constraint, once again as a plain index.
            continue
        is_partial = "postgresql_where" in idx.get("dialect_options", {})
        shapes.add((idx["name"], tuple(idx["column_names"]), idx["unique"], is_partial))
    return shapes


def _snapshot_table(inspector: Any, table_name: str) -> dict[str, Any]:
    return {
        "columns": _actual_columns(inspector, table_name),
        "pk": _actual_primary_key(inspector, table_name),
        "fks": _actual_foreign_keys(inspector, table_name),
        "uniques": _actual_unique_constraints(inspector, table_name),
        "checks": _actual_check_constraint_names(inspector, table_name),
        "indexes": _actual_indexes(inspector, table_name),
    }


def _snapshot_database(sync_connection: Connection) -> dict[str, dict[str, Any]]:
    inspector = inspect(sync_connection)
    return {name: _snapshot_table(inspector, name) for name in inspector.get_table_names()}


async def _table_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


async def _function_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT p.proname FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public'"
                )
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def _trigger_function_pairs(database_url: str) -> dict[tuple[str, str], str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT c.relname, t.tgname, p.proname "
                    "FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_proc p ON p.oid = t.tgfoid "
                    "WHERE NOT t.tgisinternal"
                )
            )
            return {(table, trigger): function for table, trigger, function in rows}
    finally:
        await engine.dispose()


def _sqlstate(exc: DBAPIError) -> str:
    """Extract the PostgreSQL SQLSTATE code from a raised `DBAPIError`."""
    return str(getattr(exc.orig, "sqlstate", ""))


async def _database_snapshot(database_url: str) -> dict[str, dict[str, Any]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_snapshot_database)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 1 & 2: exact table set, and concordance with Base.metadata.tables
# ---------------------------------------------------------------------------


def test_migrated_tables_match_expected_set_exactly(migrated_postgres_url: str) -> None:
    assert asyncio.run(_table_names(migrated_postgres_url)) == EXPECTED_TABLES


def test_every_orm_table_is_present_in_the_migrated_database(migrated_postgres_url: str) -> None:
    actual = asyncio.run(_table_names(migrated_postgres_url))
    missing = set(Base.metadata.tables) - actual
    assert not missing, f"ORM tables missing from migrated schema: {sorted(missing)}"


# ---------------------------------------------------------------------------
# 3: per-table columns / PK / FK / UNIQUE / CHECK / indexes
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema_snapshot(migrated_postgres_url: str) -> dict[str, dict[str, Any]]:
    """One inspector pass over the fully migrated database, shared by every
    per-table structural assertion below instead of reconnecting per test."""
    return asyncio.run(_database_snapshot(migrated_postgres_url))


@pytest.mark.parametrize("table_name", sorted(Base.metadata.tables))
def test_orm_table_matches_migrated_schema(
    table_name: str, schema_snapshot: dict[str, dict[str, Any]]
) -> None:
    table = Base.metadata.tables[table_name]
    actual = schema_snapshot[table_name]

    expected_columns = _expected_columns(table)
    expected_columns.update(KNOWN_COLUMN_DRIFT.get(table_name, {}))
    assert actual["columns"] == expected_columns

    assert actual["pk"] == _expected_primary_key(table)

    expected_fks = _expected_foreign_keys(table) | KNOWN_EXTRA_FOREIGN_KEYS.get(table_name, set())
    assert actual["fks"] == expected_fks

    assert actual["uniques"] == _expected_unique_constraints(table)

    expected_checks = _expected_check_constraint_names(table)
    renames = KNOWN_CHECK_CONSTRAINT_RENAMES.get(table_name, {})
    expected_checks = (expected_checks - set(renames)) | set(renames.values())
    expected_checks |= KNOWN_EXTRA_CHECK_CONSTRAINTS.get(table_name, set())
    assert actual["checks"] == expected_checks

    expected_indexes = _expected_indexes(table) | KNOWN_EXTRA_INDEXES.get(table_name, set())
    assert actual["indexes"] == expected_indexes


# ---------------------------------------------------------------------------
# 4: PostgreSQL functions and triggers
# ---------------------------------------------------------------------------


def test_migrated_database_installs_exactly_the_expected_functions(
    migrated_postgres_url: str,
) -> None:
    assert asyncio.run(_function_names(migrated_postgres_url)) == EXPECTED_FUNCTIONS


def test_migrated_database_installs_exactly_the_expected_triggers(
    migrated_postgres_url: str,
) -> None:
    actual = asyncio.run(_trigger_function_pairs(migrated_postgres_url))
    assert set(actual) == set(EXPECTED_TRIGGERS)
    assert actual == EXPECTED_TRIGGERS, "a trigger is bound to the wrong function"


# ---------------------------------------------------------------------------
# 5: guard behaviour, not just catalog presence
# ---------------------------------------------------------------------------


def test_prevent_tlp_downgrade_guard_rejects_a_downgrade(migrated_postgres_url: str) -> None:
    async def _run() -> str:
        engine = create_async_engine(migrated_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO subjects (id, external_id, slug, tlp, created_at) "
                        "VALUES (gen_random_uuid(), 'ext-1', 'subject-one', 'AMBER', now())"
                    )
                )
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE subjects SET tlp = 'CLEAR' WHERE slug = 'subject-one'")
                    )
            except DBAPIError as exc:
                return _sqlstate(exc)
            return ""
        finally:
            await engine.dispose()

    sqlstate = asyncio.run(_run())
    assert sqlstate == "23514"


def test_append_only_guard_rejects_update_and_delete(migrated_postgres_url: str) -> None:
    async def _run() -> tuple[str, str]:
        engine = create_async_engine(migrated_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO provenance_events "
                        "(id, aggregate_type, aggregate_id, event_type, payload, tlp, occurred_at) "
                        "VALUES (gen_random_uuid(), 'subject', gen_random_uuid(), 'created', "
                        "'{}'::jsonb, 'CLEAR', now())"
                    )
                )

            async def _expect_rejection(statement: str) -> str:
                try:
                    async with engine.begin() as connection:
                        await connection.execute(text(statement))
                except DBAPIError as exc:
                    return _sqlstate(exc)
                return ""

            update_sqlstate = await _expect_rejection(
                "UPDATE provenance_events SET event_type = 'tampered'"
            )
            delete_sqlstate = await _expect_rejection("DELETE FROM provenance_events")
            return update_sqlstate, delete_sqlstate
        finally:
            await engine.dispose()

    update_sqlstate, delete_sqlstate = asyncio.run(_run())
    assert update_sqlstate == "55000"
    assert delete_sqlstate == "55000"


# ---------------------------------------------------------------------------
# 6: upgrade head -> downgrade base -> upgrade head
# ---------------------------------------------------------------------------


def test_migration_up_and_down_on_temporary_postgres(temporary_postgres_url: str) -> None:
    config = _alembic_config(temporary_postgres_url)

    command.upgrade(config, "head")
    assert asyncio.run(_table_names(temporary_postgres_url)) == EXPECTED_TABLES
    assert asyncio.run(_function_names(temporary_postgres_url)) == EXPECTED_FUNCTIONS
    assert set(asyncio.run(_trigger_function_pairs(temporary_postgres_url))) == set(
        EXPECTED_TRIGGERS
    )

    command.downgrade(config, "base")
    assert asyncio.run(_table_names(temporary_postgres_url)) == {ALEMBIC_TABLE}
    assert asyncio.run(_function_names(temporary_postgres_url)) == set()
    assert asyncio.run(_trigger_function_pairs(temporary_postgres_url)) == {}

    command.upgrade(config, "head")
    assert asyncio.run(_table_names(temporary_postgres_url)) == EXPECTED_TABLES
    assert asyncio.run(_function_names(temporary_postgres_url)) == EXPECTED_FUNCTIONS
    assert asyncio.run(_trigger_function_pairs(temporary_postgres_url)) == EXPECTED_TRIGGERS


# ---------------------------------------------------------------------------
# 7: [R07a] the squashed baseline reproduces the pre-squash catalog exactly
# ---------------------------------------------------------------------------


def test_baseline_migration_reproduces_the_pre_squash_catalog_exactly(
    migrated_postgres_url: str,
) -> None:
    """The single ``0001_baseline`` migration must produce, byte-for-byte,
    the same structural catalog (tables, columns, constraints, indexes,
    sequences, functions, triggers) as the former 23-migration chain did.

    The expected catalog was captured once, with the historical chain still
    intact, and committed as a fixture (see module docstring). This is a
    structural comparison of the two manifests, not a `pg_dump` diff.
    """
    expected = json.loads(_R07A_PRE_SQUASH_CATALOG_PATH.read_text())
    actual = asyncio.run(capture_schema_catalog(migrated_postgres_url))
    assert actual == expected

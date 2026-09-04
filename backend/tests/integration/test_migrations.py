"""Exhaustive coverage of the Alembic migration chain.

This module validates, against a real (temporary) PostgreSQL database:

1. the exact set of application tables produced by ``alembic upgrade head``;
2. that this set matches ``Base.metadata.tables`` (module-level ORM models),
   exactly, with no exceptions;
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
   downgrade and fully restored after re-upgrading.

Structural comparisons are generic (driven by ``Base.metadata``) rather than
transcribed by hand, so the exhaustive checks stay correct as new migrations
and models are added instead of silently drifting out of date.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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

from cti_app.infrastructure.database.models import (  # noqa: F401
    collection,
    core,
    discovery,
    edition_publication,
    editions,
    editorial,
    invariants,
    jobs,
    model_execution,
    production,
    publication_review,
)
from cti_app.infrastructure.database.models.base import Base
from tests.integration.conftest import _alembic_config

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Expected shape of the migrated database
# ---------------------------------------------------------------------------

ALEMBIC_TABLE = "alembic_version"

EXPECTED_TABLES = frozenset(Base.metadata.tables) | {ALEMBIC_TABLE}

# (local columns, referred table, referred columns, ON DELETE) — shared by
# both the ORM-derived and the reflected-from-database foreign key shapes.
ForeignKeyShape = tuple[tuple[str, ...], str, tuple[str, ...], str | None]

# Column definitions: type family, nullability, or both.
ColumnShape = tuple[str, bool, int | None]

# (index name, ordered columns, unique, is_partial) — shared by both the
# ORM-derived and the reflected-from-database index shapes.
IndexShape = tuple[str, tuple[str, ...], bool, bool]

# (table, trigger_name) -> function_name, for every trigger installed by the
# migration chain at HEAD. This is the complete, canonical baseline installed
# by the current migration chain.
EXPECTED_TRIGGERS: dict[tuple[str, str], str] = {
    (
        "publication_review_decisions",
        "trg_publication_review_decisions_append_only",
    ): "reject_evidence_mutation",
    (
        "publication_manifests",
        "trg_publication_manifests_append_only",
    ): "reject_evidence_mutation",
    (
        "publication_manifest_entries",
        "trg_publication_manifest_entries_append_only",
    ): "reject_evidence_mutation",
    (
        "publication_manifest_exclusions",
        "trg_publication_manifest_exclusions_append_only",
    ): "reject_evidence_mutation",
    ("edition_releases", "trg_edition_releases_append_only"): "reject_evidence_mutation",
    (
        "production_input_snapshots",
        "trg_production_input_snapshots_append_only",
    ): "reject_evidence_mutation",
    (
        "production_reuse_invalidations",
        "trg_production_reuse_invalidations_append_only",
    ): "reject_evidence_mutation",
    (
        "production_repair_decisions",
        "trg_production_repair_decisions_append_only",
    ): "reject_evidence_mutation",
    ("virustotal_observations", "trg_vt_observations_append_only"): "reject_evidence_mutation",
    ("virustotal_file_views", "trg_vt_file_views_append_only"): "reject_evidence_mutation",
    ("subjects", "trg_subjects_prevent_tlp_downgrade"): "prevent_tlp_downgrade",
    ("source_documents", "trg_source_documents_prevent_tlp_downgrade"): "prevent_tlp_downgrade",
    ("samples", "trg_samples_prevent_tlp_downgrade"): "prevent_tlp_downgrade",
    ("editions", "trg_editions_prevent_tlp_downgrade"): "prevent_tlp_downgrade",
    ("provenance_events", "trg_provenance_events_append_only"): "reject_provenance_mutation",
    ("edition_audit_events", "trg_edition_audit_events_append_only"): "reject_audit_mutation",
    ("job_events", "trg_job_events_append_only"): "reject_audit_mutation",
    ("human_decisions", "trg_human_decisions_append_only"): "reject_human_decision_mutation",
    ("analyst_decisions", "trg_analyst_decisions_append_only"): "reject_evidence_mutation",
    ("analyst_input_packs", "trg_analyst_input_packs_append_only"): "reject_evidence_mutation",
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
    ("reference_members", "reference_members_immutable"): "forbid_reference_mutation",
    (
        "reference_member_disputes",
        "reference_member_disputes_immutable",
    ): "forbid_reference_mutation",
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


async def _trigger_definitions(database_url: str) -> dict[tuple[str, str], str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) "
                    "FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND NOT t.tgisinternal"
                )
            )
            return {(table, trigger): definition for table, trigger, definition in rows}
    finally:
        await engine.dispose()


async def _alembic_version(database_url: str) -> str | None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
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

    assert actual["columns"] == _expected_columns(table)

    assert actual["pk"] == _expected_primary_key(table)

    assert actual["fks"] == _expected_foreign_keys(table)

    assert actual["uniques"] == _expected_unique_constraints(table)

    assert actual["checks"] == _expected_check_constraint_names(table)

    assert actual["indexes"] == _expected_indexes(table)


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
# 6: compatibility upgrade from a database stamped at 0001
# ---------------------------------------------------------------------------


_LEGACY_ROW_IDS = {
    "blob": "10000000-0000-0000-0000-000000000001",
    "edition": "10000000-0000-0000-0000-000000000002",
    "subject": "10000000-0000-0000-0000-000000000003",
    "model_run": "10000000-0000-0000-0000-000000000004",
    "production_run": "10000000-0000-0000-0000-000000000005",
    "artifact": "10000000-0000-0000-0000-000000000006",
    "editorial_group": "10000000-0000-0000-0000-000000000007",
    "source_collection": "10000000-0000-0000-0000-000000000008",
    "source_document": "10000000-0000-0000-0000-000000000009",
    "human_decision": "10000000-0000-0000-0000-000000000010",
}
_LEGACY_CREATED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
_REPAIR_TABLE = "production_repair_decisions"
_REPAIR_TRIGGER = "trg_production_repair_decisions_append_only"
_LEGACY_TABLES = (
    ("blobs", "blob"),
    ("editions", "edition"),
    ("subjects", "subject"),
    ("model_runs", "model_run"),
    ("subject_production_runs", "production_run"),
    ("production_artifacts", "artifact"),
    ("editorial_groups", "editorial_group"),
    ("source_collections", "source_collection"),
    ("source_documents", "source_document"),
    ("human_decisions", "human_decision"),
)


async def _legacy_data_snapshot(database_url: str) -> dict[str, str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            snapshots: dict[str, str] = {}
            for table_name, id_name in _LEGACY_TABLES:
                result = await connection.execute(
                    text(
                        f"SELECT row_to_json(row)::text "
                        f"FROM (SELECT * FROM {table_name} WHERE id = :row_id) AS row"
                    ),
                    {"row_id": _LEGACY_ROW_IDS[id_name]},
                )
                snapshots[table_name] = str(result.scalar_one())
            return snapshots
    finally:
        await engine.dispose()


async def _seed_legacy_rows(database_url: str) -> dict[str, str]:
    """Seed rows that must survive the compatibility revision unchanged."""
    ids = _LEGACY_ROW_IDS
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO blobs "
                    "(id, sha256, size, mime_type, logical_bucket, object_key, created_at) "
                    "VALUES (:id, :sha256, 7, 'text/plain', 'source-raw', "
                    "'legacy/source.txt', :created_at)"
                ),
                {
                    "id": ids["blob"],
                    "sha256": "a" * 64,
                    "created_at": _LEGACY_CREATED_AT,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO editions "
                    "(id, country, country_code, period_start, period_end, tlp, languages, "
                    "target_articles, source_profile, status, version, created_at, updated_at) "
                    "VALUES (:id, 'Legacyland', 'LG', '2026-08-01', '2026-08-31', 'GREEN', "
                    "'[\"fr\"]'::jsonb, 1, 'default', 'review', 1, :created_at, :created_at)"
                ),
                {"id": ids["edition"], "created_at": _LEGACY_CREATED_AT},
            )
            await connection.execute(
                text(
                    "INSERT INTO subjects (id, external_id, slug, tlp, created_at) "
                    "VALUES (:id, 'legacy-subject', 'legacy-subject', 'GREEN', :created_at)"
                ),
                {"id": ids["subject"], "created_at": _LEGACY_CREATED_AT},
            )
            await connection.execute(
                text(
                    "INSERT INTO model_runs "
                    "(id, provider, model_role, requested_model, prompt_template_id, "
                    "prompt_template_version, authorized_input_hash, evidence_pack_hash, "
                    "parameters, status, submission_state, output_references, "
                    "validation_errors, transformations, citation_count, extracted_url_count, "
                    "visible_citations, started_at, updated_at) "
                    "VALUES (:id, 'fake', 'research', 'legacy-model', 'legacy', '1', "
                    ":input_hash, :evidence_hash, '{}'::jsonb, 'succeeded', 'not_submitted', "
                    "'[]'::jsonb, '[]'::jsonb, '[]'::jsonb, 0, 0, '[]'::jsonb, "
                    ":created_at, :created_at)"
                ),
                {
                    "id": ids["model_run"],
                    "input_hash": "b" * 64,
                    "evidence_hash": "c" * 64,
                    "created_at": _LEGACY_CREATED_AT,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO subject_production_runs "
                    "(id, subject_id, edition_id, status, current_stage, run_number, "
                    "pipeline_generation, created_at, updated_at, version) "
                    "VALUES (:id, :subject_id, :edition_id, 'queued', 'sources', 1, 0, "
                    ":created_at, :created_at, 1)"
                ),
                {
                    "id": ids["production_run"],
                    "subject_id": ids["subject"],
                    "edition_id": ids["edition"],
                    "created_at": _LEGACY_CREATED_AT,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO production_artifacts "
                    "(id, production_run_id, subject_id, stage, version, input_hash, status, "
                    '"metadata", created_at) '
                    "VALUES (:id, :run_id, :subject_id, 'extraction', 1, :input_hash, "
                    "'verified', '{}'::jsonb, :created_at)"
                ),
                {
                    "id": ids["artifact"],
                    "run_id": ids["production_run"],
                    "subject_id": ids["subject"],
                    "input_hash": "d" * 64,
                    "created_at": _LEGACY_CREATED_AT,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO editorial_groups "
                    "(id, edition_id, title, outcome, status, source_relationship_status, "
                    "needs_source_verification, needs_source_expansion, grouping_confidence, "
                    "grouping_justification, subject_id, payload, version, created_at, updated_at) "
                    "VALUES (:id, :edition_id, 'Legacy group', 'new_subject', 'selected', "
                    "'provisional', false, false, 'high', 'legacy seed', :subject_id, "
                    "'{}'::jsonb, 1, :created_at, :created_at)"
                ),
                {
                    "id": ids["editorial_group"],
                    "edition_id": ids["edition"],
                    "subject_id": ids["subject"],
                    "created_at": _LEGACY_CREATED_AT,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO source_collections "
                    "(id, subject_id, edition_id, group_id, origin_kind, requested_url, "
                    "canonical_url, title, publisher, published_at, source_tlp, sensitivity, "
                    "external_llm_allowed, do_not_submit, proposed_role, relationship_status, "
                    "relationship_evidence, state, attempt_count, created_at, updated_at) "
                    "VALUES (:id, :subject_id, :edition_id, :group_id, 'discovery', "
                    "'https://legacy.example/source', 'https://legacy.example/source', "
                    "'Legacy source', 'Legacy publisher', '2026-08-01', 'GREEN', 'normal', "
                    "true, false, 'primary', 'provisional', 'legacy seed', 'archived', 0, "
                    ":created_at, :created_at)"
                ),
                {
                    "id": ids["source_collection"],
                    "subject_id": ids["subject"],
                    "edition_id": ids["edition"],
                    "group_id": ids["editorial_group"],
                    "created_at": _LEGACY_CREATED_AT,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO source_documents "
                    "(id, subject_id, blob_id, original_name, origin, acquired_at, tlp, "
                    "do_not_submit, external_llm_allowed, created_at) "
                    "VALUES (:id, :subject_id, :blob_id, 'source.txt', "
                    "'https://legacy.example/source', :created_at, 'GREEN', false, true, "
                    ":created_at)"
                ),
                {
                    "id": ids["source_document"],
                    "subject_id": ids["subject"],
                    "blob_id": ids["blob"],
                    "created_at": _LEGACY_CREATED_AT,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO human_decisions "
                    "(id, edition_id, decision_type, group_ids, actor_id, correlation_id, "
                    "payload, occurred_at) VALUES (:id, :edition_id, 'select', '[]'::jsonb, "
                    "'legacy-operator', 'legacy-correlation', '{}'::jsonb, :created_at)"
                ),
                {
                    "id": ids["human_decision"],
                    "edition_id": ids["edition"],
                    "created_at": _LEGACY_CREATED_AT,
                },
            )

    finally:
        await engine.dispose()

    return await _legacy_data_snapshot(database_url)


async def _remove_repair_table_from_stamped_baseline(database_url: str) -> None:
    """Model a pre-Repair-Desk DB that was nevertheless stamped at 0001."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE {_REPAIR_TABLE}"))
    finally:
        await engine.dispose()


def test_legacy_0001_database_gets_repair_desk_without_data_loss(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "0001_baseline")
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0001_baseline"
    asyncio.run(_remove_repair_table_from_stamped_baseline(temporary_postgres_url))

    before_tables = asyncio.run(_table_names(temporary_postgres_url))
    before_triggers = asyncio.run(_trigger_function_pairs(temporary_postgres_url))
    assert _REPAIR_TABLE not in before_tables
    assert ("production_repair_decisions", _REPAIR_TRIGGER) not in before_triggers
    preserved = asyncio.run(_seed_legacy_rows(temporary_postgres_url))

    command.upgrade(config, "head")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0002_repair_desk_compat"
    after_tables = asyncio.run(_table_names(temporary_postgres_url))
    assert after_tables == before_tables | {_REPAIR_TABLE}
    repair_table = Base.metadata.tables[_REPAIR_TABLE]
    expected_repair_snapshot = {
        "columns": _expected_columns(repair_table),
        "pk": _expected_primary_key(repair_table),
        "fks": _expected_foreign_keys(repair_table),
        "uniques": _expected_unique_constraints(repair_table),
        "checks": _expected_check_constraint_names(repair_table),
        "indexes": _expected_indexes(repair_table),
    }
    assert asyncio.run(_database_snapshot(temporary_postgres_url))[_REPAIR_TABLE] == (
        expected_repair_snapshot
    )
    assert asyncio.run(_trigger_function_pairs(temporary_postgres_url)) == {
        **before_triggers,
        (_REPAIR_TABLE, _REPAIR_TRIGGER): "reject_evidence_mutation",
    }

    assert asyncio.run(_legacy_data_snapshot(temporary_postgres_url)) == preserved

    async def _repair_guard_sqlstates() -> tuple[str, str]:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO production_repair_decisions "
                        "(id, edition_id, subject_id, production_run_id, observed_artifact_id, "
                        "repair_key, issue_kind, action, observed_pipeline_generation, actor_id, "
                        "reason, created_at) VALUES "
                        "(:id, :edition_id, :subject_id, :run_id, :artifact_id, :repair_key, "
                        "'rejected_rule', 'include', 0, 'legacy-test', NULL, :created_at)"
                    ),
                    {
                        "id": "10000000-0000-0000-0000-000000000011",
                        "edition_id": _LEGACY_ROW_IDS["edition"],
                        "subject_id": _LEGACY_ROW_IDS["subject"],
                        "run_id": _LEGACY_ROW_IDS["production_run"],
                        "artifact_id": _LEGACY_ROW_IDS["artifact"],
                        "repair_key": "e" * 64,
                        "created_at": _LEGACY_CREATED_AT,
                    },
                )

            async def _expect_rejection(statement: str) -> str:
                try:
                    async with engine.begin() as connection:
                        await connection.execute(text(statement))
                except DBAPIError as exc:
                    return _sqlstate(exc)
                return ""

            return (
                await _expect_rejection(
                    "UPDATE production_repair_decisions SET reason = 'tampered'"
                ),
                await _expect_rejection("DELETE FROM production_repair_decisions"),
            )
        finally:
            await engine.dispose()

    assert asyncio.run(_repair_guard_sqlstates()) == ("55000", "55000")


# ---------------------------------------------------------------------------
# 7: fresh install and repeated upgrade
# ---------------------------------------------------------------------------


def test_fresh_install_and_repeated_upgrade_are_conflict_free(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)

    command.upgrade(config, "head")
    command.current(config)
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0002_repair_desk_compat"

    tables = asyncio.run(_table_names(temporary_postgres_url))
    assert _REPAIR_TABLE in tables
    assert len([table for table in tables if table == _REPAIR_TABLE]) == 1
    triggers = asyncio.run(_trigger_function_pairs(temporary_postgres_url))
    repair_triggers = {
        key: function for key, function in triggers.items() if key[0] == _REPAIR_TABLE
    }
    assert repair_triggers == {
        (_REPAIR_TABLE, _REPAIR_TRIGGER): "reject_evidence_mutation"
    }
    trigger_definitions = asyncio.run(_trigger_definitions(temporary_postgres_url))

    # 0002 must observe the table and trigger made by 0001 and perform no DDL
    # that conflicts with them.
    command.upgrade(config, "head")
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0002_repair_desk_compat"
    assert asyncio.run(_table_names(temporary_postgres_url)) == tables
    assert asyncio.run(_trigger_definitions(temporary_postgres_url)) == trigger_definitions


# ---------------------------------------------------------------------------
# 8: upgrade head -> downgrade base -> upgrade head
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

"""Exhaustive coverage of the frozen baseline and additive migrations.

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
6. that a database with no tables and no Alembic history upgrades directly
   to the current Alembic head, and that repeating ``upgrade head``
   leaves the schema and trigger definitions unchanged;
7. a full ``upgrade head`` -> ``downgrade base`` -> ``upgrade head`` cycle,
   asserting tables/functions/triggers are completely gone after the full
   downgrade and fully restored after re-upgrading.

The ORM schema is compared structurally against ``Base.metadata``. The C2
bridge additionally exercises empty, previously stamped, manually installed,
and malformed PostgreSQL schemas using disposable databases.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

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
    external_effects,
    invariants,
    investigation,
    jobs,
    model_execution,
    osint_open_policy,
    production,
    publication_render,
    publication_review,
    selection,
    source_media,
)
from cti_app.infrastructure.database.models.base import Base
from tests.integration.conftest import _alembic_config


def _head_revision() -> str:
    """Read the chain head instead of pinning one migration by name.

    The rest of this module compares the full migrated schema against
    ``Base.metadata``. The current head includes additive migrations through
    ``0007_vt_retry_trust``.
    """
    from alembic.script import ScriptDirectory

    return (
        ScriptDirectory.from_config(
            _alembic_config("postgresql://unused/unused")
        ).get_current_head()
        or ""
    )


pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Expected shape of the migrated database
# ---------------------------------------------------------------------------

ALEMBIC_TABLE = "alembic_version"
_REPAIR_TABLE = "production_repair_decisions"
_REPAIR_TRIGGER = "trg_production_repair_decisions_append_only"
INVESTIGATION_TABLES = frozenset(
    {
        "tool_invocations",
        "evidence_observations",
        "investigation_finding_versions",
        "investigation_finding_observations",
        "investigation_finding_claims",
        "sample_associations",
        "sample_acquisition_dispatch_attempts",
        "sample_association_versions",
        "sample_association_qualification_changes",
        "detection_candidate_versions",
        "detection_candidate_motifs",
        "detection_candidate_motif_observations",
        "detection_candidate_test_refs",
        "analyst_opinion_versions",
        "investigation_envelopes",
        "investigation_budget_records",
        "investigation_budget_limits",
        "investigation_budget_reservations",
        "investigation_budget_reservation_amounts",
        "investigation_working_states",
        "investigation_experiment_heads",
        "investigation_experiments",
        "osint_open_policies",
        "osint_open_policy_events",
        "osint_open_inheritances",
    }
)

EXPECTED_TABLES = frozenset(Base.metadata.tables) | {ALEMBIC_TABLE, "discovery_candidates"}


def test_investigation_tables_are_registered_in_orm_metadata() -> None:
    assert INVESTIGATION_TABLES <= set(Base.metadata.tables)


# (local columns, referred table, referred columns, ON DELETE) — shared by
# both the ORM-derived and the reflected-from-database foreign key shapes.
ForeignKeyShape = tuple[tuple[str, ...], str, tuple[str, ...], str | None]

# Column definitions: type family, nullability, or both.
ColumnShape = tuple[str, bool, int | None]

# (index name, ordered columns, unique, is_partial) — shared by both the
# ORM-derived and the reflected-from-database index shapes.
IndexShape = tuple[str, tuple[str, ...], bool, bool]

# (table, trigger_name) -> function_name, for every trigger installed at HEAD.
EXPECTED_TRIGGERS: dict[tuple[str, str], str] = {
    (
        "osint_open_policy_events",
        "trg_osint_open_policy_events_append_only",
    ): "reject_osint_open_policy_event_mutation",
    (
        "osint_open_policies",
        "trg_osint_open_policies_immutable",
    ): "reject_osint_open_policy_version_mutation",
    (
        "edition_composition_events",
        "trg_edition_composition_events_append_only",
    ): "reject_evidence_mutation",
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
    # A correction is the immutable replacement payload of a REPLACE decision:
    # rewriting it would rewrite the audit of what the analyst actually chose.
    (
        "production_repair_corrections",
        "trg_production_repair_corrections_append_only",
    ): "reject_evidence_mutation",
    (
        "production_source_additions",
        "trg_production_source_additions_append_only",
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
    (
        "investigation_experiments",
        "trg_investigation_experiments_append_only",
    ): "reject_investigation_experiment_mutation",
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
        "discovery_runs",
        "trg_discovery_runs_append_only",
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
    ("selection_decisions", "trg_selection_decisions_append_only"): "reject_selection_mutation",
    (
        "subject_discovery_origins",
        "trg_subject_discovery_origins_append_only",
    ): "reject_selection_mutation",
    (
        "selection_idempotency_records",
        "trg_selection_idempotency_records_append_only",
    ): "reject_selection_mutation",
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
        referred_table = str(elements[0].target_fullname).split(".", 1)[0]
        shapes.add(
            (
                tuple(fkc.column_keys),
                referred_table,
                tuple(str(elem.target_fullname).split(".", 1)[1] for elem in elements),
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


async def _check_constraint_definitions(database_url: str, table_name: str) -> dict[str, str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: {
                    str(check["name"]): str(check["sqltext"])
                    for check in inspect(sync).get_check_constraints(table_name)
                }
            )
    finally:
        await engine.dispose()


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


async def _function_definitions(database_url: str) -> dict[str, str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT p.proname, pg_get_functiondef(p.oid) "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public'"
                )
            )
            return {name: definition for name, definition in rows}
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


async def _check_constraints(database_url: str, table_name: str) -> dict[str, str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: {
                    str(check["name"]): str(check["sqltext"])
                    for check in inspect(sync).get_check_constraints(table_name)
                }
            )
    finally:
        await engine.dispose()


async def _c2_catalog_snapshot(database_url: str) -> dict[str, Any]:
    schema = await _database_snapshot(database_url)
    functions = await _function_definitions(database_url)
    triggers = await _trigger_definitions(database_url)
    c2_table_names = {"investigation_experiment_heads", "investigation_experiments"}
    return {
        "tables": {name: schema[name] for name in sorted(c2_table_names) if name in schema},
        "function": functions.get("reject_investigation_experiment_mutation"),
        "triggers": {key: value for key, value in triggers.items() if key[0] in c2_table_names},
    }


async def _insert_edition_marker(database_url: str, edition_id: Any) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO editions "
                    "(id, country, country_code, period_start, period_end, tlp, languages, "
                    "state, version, created_at, updated_at) "
                    "VALUES (:id, 'Migration fixture', 'FR', DATE '2026-10-01', "
                    "DATE '2026-10-31', 'CLEAR', CAST(:languages AS jsonb), 'open', 1, "
                    "now(), now())"
                ),
                {"id": edition_id, "languages": json.dumps(["fr"])},
            )
            result = await connection.execute(
                text("SELECT country FROM editions WHERE id = :id"), {"id": edition_id}
            )
            return str(result.scalar_one())
    finally:
        await engine.dispose()


async def _install_manual_c2_ddl(database_url: str) -> None:
    # Exact copy of the documented 2026-10-19 DDL, executed on a disposable DB.
    statements = (
        """
        CREATE TABLE investigation_experiment_heads (
            investigation_id uuid PRIMARY KEY
                REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
            version bigint NOT NULL
                CONSTRAINT ck_investigation_experiment_heads_version CHECK (version >= 0),
            updated_at timestamptz NOT NULL
        )
        """,
        """
        CREATE TABLE investigation_experiments (
            id uuid PRIMARY KEY,
            investigation_id uuid NOT NULL
                REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
            version bigint NOT NULL
                CONSTRAINT ck_investigation_experiments_version CHECK (version >= 1),
            hypothesis_id varchar(128) NOT NULL,
            claim_ids jsonb NOT NULL,
            schema_version varchar(64) NOT NULL
                CONSTRAINT ck_investigation_experiments_schema_version
                    CHECK (schema_version = 'investigation-experiment.v1'),
            payload jsonb NOT NULL,
            payload_sha256 varchar(64) NOT NULL
                CONSTRAINT ck_investigation_experiments_payload_sha256
                    CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            recorded_at timestamptz NOT NULL,
            CONSTRAINT uq_investigation_experiments_version
                UNIQUE (investigation_id, version),
            CONSTRAINT ck_investigation_experiments_payload_identity CHECK (
                payload->>'schema_version' = schema_version
                AND payload->>'id' = id::text
                AND payload->>'investigation_id' = investigation_id::text
                AND payload->>'version' = version::text
            ),
            CONSTRAINT ck_investigation_experiments_payload_shape CHECK (
                jsonb_typeof(payload) = 'object'
                AND jsonb_typeof(claim_ids) = 'array'
            )
        )
        """,
        """
        CREATE INDEX ix_investigation_experiments_claim_timeline
            ON investigation_experiments (investigation_id, hypothesis_id, version)
        """,
        """
        CREATE FUNCTION reject_investigation_experiment_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'investigation_experiments is append-only'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE TRIGGER trg_investigation_experiments_append_only
            BEFORE UPDATE OR DELETE ON investigation_experiments
            FOR EACH ROW EXECUTE FUNCTION reject_investigation_experiment_mutation()
        """,
    )
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _insert_experiment_without_parent(database_url: str) -> None:
    experiment_id = uuid4()
    investigation_id = uuid4()
    payload = json.dumps(
        {
            "schema_version": "investigation-experiment.v1",
            "id": str(experiment_id),
            "investigation_id": str(investigation_id),
            "version": 1,
        }
    )
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            # This disposable fixture has no investigation parent. Remove the
            # FK to exercise the downgrade data guard with one persisted row.
            await connection.execute(
                text(
                    "ALTER TABLE investigation_experiments "
                    "DROP CONSTRAINT investigation_experiments_investigation_id_fkey"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO investigation_experiments "
                    "(id, investigation_id, version, hypothesis_id, claim_ids, schema_version, "
                    "payload, payload_sha256, recorded_at) "
                    "VALUES (:id, :investigation_id, 1, 'migration-guard', CAST('[]' AS jsonb), "
                    "'investigation-experiment.v1', CAST(:payload AS jsonb), :sha256, now())"
                ),
                {
                    "id": experiment_id,
                    "investigation_id": investigation_id,
                    "payload": payload,
                    "sha256": "a" * 64,
                },
            )
    finally:
        await engine.dispose()


async def _edition_schema(database_url: str) -> tuple[set[str], dict[str, str], set[str]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:

            def inspect_edition(
                sync_connection: Connection,
            ) -> tuple[set[str], dict[str, str], set[str]]:
                inspector = inspect(sync_connection)
                columns = {column["name"] for column in inspector.get_columns("editions")}
                checks = {
                    str(check["name"]): str(check.get("sqltext", ""))
                    for check in inspector.get_check_constraints("editions")
                }
                indexes = {str(index["name"]) for index in inspector.get_indexes("editions")}
                return columns, checks, indexes

            return await connection.run_sync(inspect_edition)
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


def test_production_repair_decision_source_loss_contract_is_in_fresh_schema(
    migrated_postgres_url: str,
    schema_snapshot: dict[str, dict[str, Any]],
) -> None:
    decision_schema = schema_snapshot[_REPAIR_TABLE]
    assert {
        "source_criticality",
        "criticality_reason",
        "failure_code",
        "criticality_policy_version",
    } <= set(decision_schema["columns"])
    assert {
        "ck_production_repair_issue_kind",
        "ck_production_repair_action_compatibility",
        "ck_production_repair_source_criticality",
        "ck_production_repair_criticality_reason",
        "ck_production_repair_source_criticality_pair",
        "ck_production_repair_criticality_policy_version",
        "ck_production_repair_source_extraction_diagnostic",
    } <= decision_schema["checks"]

    checks = asyncio.run(_check_constraint_definitions(migrated_postgres_url, _REPAIR_TABLE))
    assert "source_extraction_failed" in checks["ck_production_repair_issue_kind"].lower()
    assert "continue_without_source" in checks["ck_production_repair_action_compatibility"].lower()
    assert (
        "failure_code is not null"
        in checks["ck_production_repair_source_extraction_diagnostic"].lower()
    )


def test_fusion_schema_uses_canonical_candidate_identity(
    schema_snapshot: dict[str, dict[str, Any]],
) -> None:
    candidates = schema_snapshot["discovery_candidates"]
    assert candidates["pk"] == frozenset({"id"})
    assert frozenset({"discovery_batch_id", "position"}) in candidates["uniques"]
    assert (
        ("supersedes_candidate_id",),
        "discovery_candidates",
        ("id",),
        "RESTRICT",
    ) in candidates["fks"]
    # Un candidat n'est remplacé que par une seule correction. En PostgreSQL
    # les NULL restent distincts dans une contrainte UNIQUE, donc la contrainte
    # simple exprime exactement la même règle qu'un index partiel.
    assert frozenset({"supersedes_candidate_id"}) in candidates["uniques"]

    contributions = schema_snapshot["subject_contributions"]
    assert "candidate_key" not in contributions["columns"]
    assert frozenset({"candidate_id"}) in contributions["uniques"]
    assert (("candidate_id",), "discovery_candidates", ("id",), "RESTRICT") in contributions["fks"]
    # Structural human decisions (merge/split) have no intake.
    assert schema_snapshot["discovery_snapshots"]["columns"]["intake_id"][1] is True
    assert schema_snapshot["discovery_merge_runs"]["columns"]["intake_id"][1] is True
    assert not {"fusion_groups", "candidate_groups"} & set(schema_snapshot)


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
                        "WITH edition AS ("
                        "INSERT INTO editions ("
                        "id, country, country_code, period_start, period_end, tlp, languages, "
                        "state, version, created_at, updated_at"
                        ") VALUES ("
                        "gen_random_uuid(), 'France', 'FR', DATE '2026-01-01', "
                        "DATE '2026-01-31', 'AMBER', '[\"fr\"]'::jsonb, 'open', 1, now(), now()"
                        ") RETURNING id) "
                        "INSERT INTO subjects ("
                        "id, edition_id, title, slug, tlp, version, created_at, updated_at"
                        ") SELECT gen_random_uuid(), id, 'Subject One', 'subject-one', 'AMBER', "
                        "1, now(), now() FROM edition"
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


def test_discovery_run_append_only_guard_rejects_update(migrated_postgres_url: str) -> None:
    async def _run() -> str:
        engine = create_async_engine(migrated_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "WITH edition AS ("
                        "INSERT INTO editions ("
                        "id, country, country_code, period_start, period_end, tlp, languages, "
                        "state, version, created_at, updated_at"
                        ") VALUES ("
                        "gen_random_uuid(), 'Zzland', 'ZZ', DATE '2029-11-01', "
                        "DATE '2029-11-30', 'AMBER', '[\"fr\"]'::jsonb, 'open', 1, now(), now()"
                        ") RETURNING id) "
                        "INSERT INTO discovery_runs ("
                        "id, edition_id, input_mode, source_profile, complementary_axis, "
                        "request_snapshot, idempotency_key, created_by, created_at"
                        ") SELECT gen_random_uuid(), id, 'bridge_research', 'default-v1', "
                        "'initial', '{}'::jsonb, 'migration-test', 'analyst-1', now() FROM edition"
                    )
                )
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE discovery_runs SET created_by = 'tampered'")
                    )
            except DBAPIError as exc:
                return _sqlstate(exc)
            return ""
        finally:
            await engine.dispose()

    assert asyncio.run(_run()) == "55000"


def test_batch_pause_requires_timestamp_and_actor_identity(
    migrated_postgres_url: str,
) -> None:
    async def _run() -> tuple[str, str]:
        engine = create_async_engine(migrated_postgres_url)
        try:
            async with engine.begin() as connection:
                edition_id = (
                    await connection.execute(
                        text(
                            "INSERT INTO editions ("
                            "id, country, country_code, period_start, period_end, tlp, languages, "
                            "state, version, created_at, updated_at"
                            ") VALUES (gen_random_uuid(), 'Pause Test', 'PB', DATE '2026-04-01', "
                            "DATE '2026-04-30', 'AMBER', '[\"fr\"]'::jsonb, "
                            "'open', 1, now(), now()) "
                            "RETURNING id"
                        )
                    )
                ).scalar_one()
                await connection.execute(
                    text(
                        "INSERT INTO edition_production_batches ("
                        "id, edition_id, idempotency_key, request_fingerprint, actor_id, "
                        "correlation_id, status, phase, created_at, paused_at, paused_by, version"
                        ") VALUES (gen_random_uuid(), :edition_id, 'pause-valid', repeat('a', 64), "
                        "'migration-test', 'pause-valid', 'paused', 'initial', now(), now(), "
                        "'analyst-1', 1)"
                    ),
                    {"edition_id": edition_id},
                )

            async def _expect_rejection(
                idempotency_key: str, paused_at: str, paused_by: str
            ) -> str:
                try:
                    async with engine.begin() as connection:
                        await connection.execute(
                            text(
                                "INSERT INTO edition_production_batches ("
                                "id, edition_id, idempotency_key, request_fingerprint, actor_id, "
                                "correlation_id, status, phase, created_at, paused_at, paused_by, "
                                "version"
                                f") VALUES (gen_random_uuid(), :edition_id, :idempotency_key, "
                                f"repeat('b', 64), 'migration-test', :idempotency_key, 'paused', "
                                f"'initial', now(), {paused_at}, {paused_by}, 1)"
                            ),
                            {
                                "edition_id": edition_id,
                                "idempotency_key": idempotency_key,
                            },
                        )
                except DBAPIError as exc:
                    return _sqlstate(exc)
                return ""

            missing_actor = await _expect_rejection("pause-missing-actor", "now()", "NULL")
            missing_timestamp = await _expect_rejection(
                "pause-missing-timestamp", "NULL", "'analyst-1'"
            )
            return missing_actor, missing_timestamp
        finally:
            await engine.dispose()

    assert asyncio.run(_run()) == ("23514", "23514")


# ---------------------------------------------------------------------------
# 6: fresh install and repeated upgrade
# ---------------------------------------------------------------------------


def test_fresh_install_rejects_an_incompatible_existing_schema(
    temporary_postgres_url: str,
) -> None:
    async def _create_conflicting_table() -> None:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TABLE editions (id uuid PRIMARY KEY)"))
        finally:
            await engine.dispose()

    asyncio.run(_create_conflicting_table())
    with pytest.raises(DBAPIError) as exc_info:
        command.upgrade(_alembic_config(temporary_postgres_url), "head")

    assert _sqlstate(exc_info.value) == "42P07"
    assert "editions" in str(exc_info.value).lower()


def test_fresh_install_and_repeated_upgrade_are_conflict_free(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)

    assert asyncio.run(_table_names(temporary_postgres_url)) == set()
    command.upgrade(config, "head")
    command.current(config)
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == _head_revision()
    assert _head_revision() == "0007_vt_retry_trust"

    tables = asyncio.run(_table_names(temporary_postgres_url))
    assert _REPAIR_TABLE in tables
    assert len([table for table in tables if table == _REPAIR_TABLE]) == 1
    edition_columns, edition_checks, edition_indexes = asyncio.run(
        _edition_schema(temporary_postgres_url)
    )
    assert edition_columns == {
        "id",
        "country",
        "country_code",
        "period_start",
        "period_end",
        "tlp",
        "languages",
        "state",
        "version",
        "created_at",
        "updated_at",
    }

    assert not edition_columns & {
        "target_articles",
        "previous_edition_id",
        "source_profile",
        "status",
    }
    state_check = edition_checks["ck_editions_state"].lower()
    assert "'open'" in state_check
    assert "'archived'" in state_check
    assert "status" not in state_check
    assert "ix_editions_country_state" in edition_indexes
    assert "ix_editions_country_status" not in edition_indexes
    triggers = asyncio.run(_trigger_function_pairs(temporary_postgres_url))
    repair_triggers = {
        key: function for key, function in triggers.items() if key[0] == _REPAIR_TABLE
    }
    assert repair_triggers == {(_REPAIR_TABLE, _REPAIR_TRIGGER): "reject_evidence_mutation"}
    schema_definitions = asyncio.run(_database_snapshot(temporary_postgres_url))
    batch_item_schema = schema_definitions["edition_production_batch_items"]
    assert "verified_reemission_count" in batch_item_schema["columns"]
    assert "ck_batch_item_verified_reemission_count" in batch_item_schema["checks"]
    publication_manifest_schema = schema_definitions["publication_manifests"]
    assert (
        frozenset({"edition_id", "edition_version"}) not in publication_manifest_schema["uniques"]
    )
    assert "ck_publication_manifest_edition_version" in publication_manifest_schema["checks"]
    assert "ix_publication_manifests_edition_created" in {
        index[0] for index in publication_manifest_schema["indexes"]
    }
    trigger_definitions = asyncio.run(_trigger_definitions(temporary_postgres_url))

    # The migration chain is idempotent: repeating the upgrade is a no-op for
    # the schema and trigger definitions.
    command.upgrade(config, "head")
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == _head_revision()
    assert asyncio.run(_table_names(temporary_postgres_url)) == tables
    assert asyncio.run(_database_snapshot(temporary_postgres_url)) == schema_definitions
    assert asyncio.run(_trigger_definitions(temporary_postgres_url)) == trigger_definitions


def test_tool_seconds_migration_updates_the_existing_reservation_check(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "0005_sample_acquisition_dispatch")

    async def _restore_pre_0006_check() -> None:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE investigation_budget_reservation_amounts "
                        "DROP CONSTRAINT ck_budget_reservation_amounts_values"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE investigation_budget_reservation_amounts "
                        "ADD CONSTRAINT ck_budget_reservation_amounts_values CHECK ("
                        "(reserved_amount IS NULL OR reserved_amount >= 0) AND "
                        "(committed_amount IS NULL OR committed_amount >= 0) AND "
                        "(reserved_amount IS NOT NULL OR committed_amount IS NOT NULL) AND "
                        "(committed_amount IS NULL OR "
                        "committed_amount <= coalesce(reserved_amount, 0)))"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_restore_pre_0006_check())
    command.upgrade(config, "head")

    checks = asyncio.run(
        _check_constraints(temporary_postgres_url, "investigation_budget_reservation_amounts")
    )
    tool_seconds_check = checks["ck_budget_reservation_amounts_values"]
    normalized = tool_seconds_check.lower()
    # PostgreSQL may render the comparison as ``dimension::text = 'tool_seconds'::text``.
    assert "'tool_seconds'" in normalized
    assert "committed_amount <= coalesce" in normalized


def _assert_c2_absent(database_url: str) -> None:
    tables = asyncio.run(_table_names(database_url))
    assert not {"investigation_experiment_heads", "investigation_experiments"} & tables
    assert "reject_investigation_experiment_mutation" not in asyncio.run(
        _function_names(database_url)
    )
    triggers = asyncio.run(_trigger_function_pairs(database_url))
    assert not any(
        table in {"investigation_experiment_heads", "investigation_experiments"}
        for table, _name in triggers
    )


def test_stamped_0001_without_c2_upgrades_additively_and_preserves_data(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "0001_baseline")
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0001_baseline"
    _assert_c2_absent(temporary_postgres_url)

    edition_id = uuid4()
    marker = asyncio.run(_insert_edition_marker(temporary_postgres_url, edition_id))
    assert marker == "Migration fixture"
    before = asyncio.run(_database_snapshot(temporary_postgres_url))

    command.upgrade(config, "0004_external_effect_claims")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0004_external_effect_claims"
    at_0004 = asyncio.run(_database_snapshot(temporary_postgres_url))
    for table_name, table_schema in before.items():
        if table_name == "investigation_envelopes":
            continue
        assert at_0004[table_name] == table_schema

    command.upgrade(config, "head")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0007_vt_retry_trust"
    after = asyncio.run(_database_snapshot(temporary_postgres_url))
    assert {
        "authority_kind",
        "inherited_policy_id",
        "inherited_policy_version",
        "inherited_policy_event_id",
    } <= set(after["investigation_envelopes"]["columns"])
    assert {"investigation_experiment_heads", "investigation_experiments"} <= set(after)
    assert "dispatch_attempt_id" in after["sample_acquisition_attempts"]["columns"]
    assert any(
        index[0] == "uq_sample_acquisition_attempt_dispatch"
        for index in after["sample_acquisition_attempts"]["indexes"]
    )
    assert asyncio.run(_insert_edition_marker_read(temporary_postgres_url, edition_id)) == (
        "Migration fixture"
    )


def test_stamped_0001_with_exact_manual_c2_schema_advances_without_ddl(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "0001_baseline")
    _assert_c2_absent(temporary_postgres_url)
    edition_id = uuid4()
    asyncio.run(_insert_edition_marker(temporary_postgres_url, edition_id))
    asyncio.run(_install_manual_c2_ddl(temporary_postgres_url))
    before_c2 = asyncio.run(_c2_catalog_snapshot(temporary_postgres_url))
    before_tables = asyncio.run(_database_snapshot(temporary_postgres_url))

    command.upgrade(config, "0004_external_effect_claims")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0004_external_effect_claims"
    at_0004 = asyncio.run(_database_snapshot(temporary_postgres_url))
    for table_name, table_schema in before_tables.items():
        if table_name == "investigation_envelopes":
            continue
        assert at_0004[table_name] == table_schema

    command.upgrade(config, "head")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0007_vt_retry_trust"
    assert asyncio.run(_c2_catalog_snapshot(temporary_postgres_url)) == before_c2
    after_tables = asyncio.run(_database_snapshot(temporary_postgres_url))
    assert {
        "authority_kind",
        "inherited_policy_id",
        "inherited_policy_version",
        "inherited_policy_event_id",
    } <= set(after_tables["investigation_envelopes"]["columns"])
    assert "dispatch_attempt_id" in after_tables["sample_acquisition_attempts"]["columns"]
    assert any(
        index[0] == "uq_sample_acquisition_attempt_dispatch"
        for index in after_tables["sample_acquisition_attempts"]["indexes"]
    )
    assert asyncio.run(_insert_edition_marker_read(temporary_postgres_url, edition_id)) == (
        "Migration fixture"
    )


def test_stamped_0001_with_malformed_manual_c2_is_rejected_without_changes(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "0001_baseline")
    asyncio.run(_install_manual_c2_ddl(temporary_postgres_url))

    async def _alter_manual_schema() -> None:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE investigation_experiment_heads "
                        "ALTER COLUMN version TYPE integer"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_alter_manual_schema())
    before_c2 = asyncio.run(_c2_catalog_snapshot(temporary_postgres_url))
    before_tables = asyncio.run(_database_snapshot(temporary_postgres_url))

    with pytest.raises(RuntimeError, match="C2 migration preflight rejected") as exc_info:
        command.upgrade(config, "head")

    assert "investigation_experiment_heads.version" in str(exc_info.value)
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0001_baseline"
    assert asyncio.run(_c2_catalog_snapshot(temporary_postgres_url)) == before_c2
    assert asyncio.run(_database_snapshot(temporary_postgres_url)) == before_tables


async def _insert_edition_marker_read(database_url: str, edition_id: Any) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT country FROM editions WHERE id = :id"), {"id": edition_id}
            )
            return str(result.scalar_one())
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 7: upgrade head -> downgrade base -> upgrade head
# ---------------------------------------------------------------------------


def test_migration_up_and_down_on_temporary_postgres(temporary_postgres_url: str) -> None:
    config = _alembic_config(temporary_postgres_url)

    command.upgrade(config, "head")
    assert asyncio.run(_table_names(temporary_postgres_url)) == EXPECTED_TABLES
    assert asyncio.run(_function_names(temporary_postgres_url)) == EXPECTED_FUNCTIONS
    assert set(asyncio.run(_trigger_function_pairs(temporary_postgres_url))) == set(
        EXPECTED_TRIGGERS
    )

    command.downgrade(config, "0004_external_effect_claims")
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0004_external_effect_claims"
    assert asyncio.run(_table_names(temporary_postgres_url)) == (
        EXPECTED_TABLES
        - {"sample_acquisition_dispatch_attempts", "effect_acquisition_dispatch_links"}
    )
    at_0004 = asyncio.run(_database_snapshot(temporary_postgres_url))
    assert "dispatch_attempt_id" not in at_0004["sample_acquisition_attempts"]["columns"]
    assert not any(
        index[0] == "uq_sample_acquisition_attempt_dispatch"
        for index in at_0004["sample_acquisition_attempts"]["indexes"]
    )

    command.upgrade(config, "head")
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0007_vt_retry_trust"
    assert asyncio.run(_table_names(temporary_postgres_url)) == EXPECTED_TABLES

    command.downgrade(config, "base")
    assert asyncio.run(_table_names(temporary_postgres_url)) == {ALEMBIC_TABLE}
    assert asyncio.run(_function_names(temporary_postgres_url)) == set()
    assert asyncio.run(_trigger_function_pairs(temporary_postgres_url)) == {}

    command.upgrade(config, "head")
    assert asyncio.run(_table_names(temporary_postgres_url)) == EXPECTED_TABLES
    assert asyncio.run(_function_names(temporary_postgres_url)) == EXPECTED_FUNCTIONS
    assert asyncio.run(_trigger_function_pairs(temporary_postgres_url)) == EXPECTED_TRIGGERS


def test_c2_downgrade_refuses_to_discard_experiment_rows(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    asyncio.run(_insert_experiment_without_parent(temporary_postgres_url))

    with pytest.raises(RuntimeError, match="refusing to downgrade C2 experiment storage"):
        command.downgrade(config, "0001_baseline")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0007_vt_retry_trust"

    async def _experiment_count() -> int:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT count(*) FROM investigation_experiments")
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(_experiment_count()) == 1


def test_osint_policy_downgrade_refuses_policy_and_inherited_lineage_rows(
    temporary_postgres_url: str,
) -> None:
    from cti_app.infrastructure.database.session import (
        create_postgres_engine,
        create_session_factory,
    )
    from tests.integration.test_osint_open_policy_postgres import (
        _activate_for_subject,
        _make_investigation,
        _service,
        _start_with_policy,
        _uow_factory,
    )

    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    engine = create_postgres_engine(temporary_postgres_url)
    session_factory = create_session_factory(engine)
    service = _service(session_factory)

    async def _create_inherited_envelope() -> None:
        try:
            investigation_case = await _make_investigation(session_factory)
            await _activate_for_subject(service, investigation_case.subject_id)
            envelope = await _start_with_policy(
                service, _uow_factory(session_factory), investigation_case
            )
            assert envelope is not None
            assert envelope.inherited_policy_event_id is not None
        finally:
            await engine.dispose()

    async def _state_counts() -> tuple[int, int, int, int, int, int]:
        query_engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with query_engine.connect() as connection:
                counts = []
                for table_name in (
                    "osint_open_policies",
                    "osint_open_policy_events",
                    "osint_open_inheritances",
                    "investigation_envelopes",
                ):
                    result = await connection.execute(text(f"SELECT count(*) FROM {table_name}"))
                    counts.append(int(result.scalar_one()))
                lineage = await connection.execute(
                    text(
                        "SELECT count(*) FROM investigation_envelopes "
                        "WHERE authority_kind = 'osint_policy' "
                        "AND inherited_policy_event_id IS NOT NULL"
                    )
                )
                counts.append(int(lineage.scalar_one()))
                policy_event = await connection.execute(
                    text(
                        "SELECT count(*) FROM investigation_envelopes e "
                        "JOIN osint_open_policy_events p "
                        "ON p.id = e.inherited_policy_event_id "
                        "AND p.policy_id = e.inherited_policy_id "
                        "AND p.policy_version = e.inherited_policy_version"
                    )
                )
                counts.append(int(policy_event.scalar_one()))
                return tuple(counts)  # type: ignore[return-value]
        finally:
            await query_engine.dispose()

    asyncio.run(_create_inherited_envelope())
    with pytest.raises(RuntimeError, match="Cannot downgrade populated OSINT_OPEN policy storage"):
        command.downgrade(config, "0002_c2_experiments")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0007_vt_retry_trust"
    assert asyncio.run(_state_counts()) == (1, 1, 1, 1, 1, 1)


def test_0002_envelope_null_authority_is_read_as_human_without_backfill(
    temporary_postgres_url: str,
) -> None:
    from cti_app.domain.investigation import EnvelopeAuthorityKind
    from cti_app.infrastructure.database.session import (
        create_postgres_engine,
        create_session_factory,
    )
    from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from tests.integration.test_osint_open_policy_postgres import _make_investigation

    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "0002_c2_experiments")
    engine = create_postgres_engine(temporary_postgres_url)
    session_factory = create_session_factory(engine)

    async def _prepare_legacy_envelope() -> Any:
        try:
            investigation_case = await _make_investigation(session_factory)
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO investigation_envelopes "
                        "(investigation_id, allowed_providers, budget_snapshot, "
                        "acquire_hits_allowed, disclosure_tlp, disclosure_do_not_submit, "
                        "disclosure_external_llm_allowed, approval_decision_id, approval_actor_id, "
                        "approved_at, updated_at, version) "
                        "VALUES (:investigation_id, CAST('[]' AS jsonb), "
                        'CAST(\'{"limits": {}, "reservations": []}\' AS jsonb), '
                        "false, 'green', false, false, :decision_id, 'legacy-actor', "
                        "now(), now(), 1)"
                    ),
                    {"investigation_id": investigation_case.id, "decision_id": uuid4()},
                )
            return investigation_case.id
        finally:
            await engine.dispose()

    investigation_id = asyncio.run(_prepare_legacy_envelope())
    command.upgrade(config, "head")

    read_engine = create_postgres_engine(temporary_postgres_url)
    read_session_factory = create_session_factory(read_engine)

    async def _read_legacy_envelope() -> tuple[str | None, EnvelopeAuthorityKind | None]:
        try:
            async with read_engine.connect() as connection:
                raw_authority = await connection.scalar(
                    text(
                        "SELECT authority_kind FROM investigation_envelopes "
                        "WHERE investigation_id = :id"
                    ),
                    {"id": investigation_id},
                )
            async with SqlAlchemyUnitOfWork(read_session_factory) as uow:
                envelope = await uow.investigation_envelopes.get(investigation_id)
                assert envelope is not None
                return raw_authority, envelope.authority_kind
        finally:
            await read_engine.dispose()

    raw_authority, interpreted_authority = asyncio.run(_read_legacy_envelope())
    assert raw_authority is None
    assert interpreted_authority is EnvelopeAuthorityKind.HUMAN_G2
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0007_vt_retry_trust"

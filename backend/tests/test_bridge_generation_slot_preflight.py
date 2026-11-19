from __future__ import annotations

import importlib.util
from pathlib import Path

_MIGRATION_PATH = (
    Path(__file__).parents[1] / "migrations" / "versions" / "0013_bridge_generation_slots.py"
)
_SPEC = importlib.util.spec_from_file_location("bridge_generation_slots_migration", _MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)


def _constraint(
    name: str,
    contype: str,
    columns: tuple[str, ...],
    definition: str | None = None,
    *,
    referenced_table: str | None = None,
    referenced_columns: tuple[str, ...] = (),
    confdeltype: str | None = None,
    confupdtype: str | None = None,
    confmatchtype: str | None = None,
) -> dict[str, object]:
    return {
        "conname": name,
        "contype": contype,
        "convalidated": True,
        "condeferrable": False,
        "condeferred": False,
        "table_schema": "public",
        "table_name": "bridge_generation_slots",
        "columns": list(columns),
        "referenced_schema": "public" if referenced_table else None,
        "referenced_table": referenced_table,
        "referenced_columns": list(referenced_columns),
        "confdeltype": confdeltype,
        "confupdtype": confupdtype,
        "confmatchtype": confmatchtype,
        "definition": definition,
    }


def _canonical_shape() -> dict[str, object]:
    return {
        "relation": {"relkind": "r", "relpersistence": "p"},
        "table_type_exists": True,
        "columns": [
            {
                "column_name": "singleton_key",
                "column_type": "smallint",
                "attnotnull": True,
                "default_expression": None,
            },
            {
                "column_name": "holder_thread_id",
                "column_type": "uuid",
                "attnotnull": False,
                "default_expression": None,
            },
            {
                "column_name": "holder_job_id",
                "column_type": "uuid",
                "attnotnull": False,
                "default_expression": None,
            },
            {
                "column_name": "holder_turn_id",
                "column_type": "uuid",
                "attnotnull": False,
                "default_expression": None,
            },
            {
                "column_name": "lease_token",
                "column_type": "bigint",
                "attnotnull": True,
                "default_expression": "0",
            },
            {
                "column_name": "acquired_at",
                "column_type": "timestamp with time zone",
                "attnotnull": False,
                "default_expression": None,
            },
            {
                "column_name": "lease_expires_at",
                "column_type": "timestamp with time zone",
                "attnotnull": False,
                "default_expression": None,
            },
            {
                "column_name": "state",
                "column_type": "character varying(16)",
                "attnotnull": True,
                "default_expression": "'free'::character varying",
            },
        ],
        "constraints": [
            _constraint("bridge_generation_slots_pkey", "p", ("singleton_key",)),
            _constraint(
                "ck_bridge_generation_slots_holder_state",
                "c",
                (
                    "state",
                    "holder_thread_id",
                    "holder_job_id",
                    "holder_turn_id",
                    "acquired_at",
                    "lease_expires_at",
                ),
                "CHECK (((state)::text = 'free'::text AND holder_thread_id IS NULL "
                "AND holder_job_id IS NULL AND holder_turn_id IS NULL AND acquired_at IS NULL "
                "AND lease_expires_at IS NULL) OR ((state)::text = ANY "
                "((ARRAY['held'::character varying, 'held_unknown'::character varying])::text[]) "
                "AND holder_thread_id IS NOT NULL AND holder_job_id IS NOT NULL "
                "AND acquired_at IS NOT NULL AND lease_expires_at IS NOT NULL))",
            ),
            _constraint(
                "ck_bridge_generation_slots_singleton",
                "c",
                ("singleton_key",),
                "CHECK ((singleton_key = 1))",
            ),
            _constraint(
                "ck_bridge_generation_slots_state",
                "c",
                ("state",),
                "CHECK ((state)::text = ANY (ARRAY['free'::character varying, "
                "'held'::character varying, 'held_unknown'::character varying]::text[]))",
            ),
            _constraint(
                "ck_bridge_generation_slots_token",
                "c",
                ("lease_token",),
                "CHECK ((lease_token >= 0))",
            ),
            _constraint(
                "fk_bridge_generation_slots_job",
                "f",
                ("holder_job_id",),
                referenced_table="editorial_thread_jobs",
                referenced_columns=("id",),
                confdeltype="r",
                confupdtype="a",
                confmatchtype="s",
            ),
            _constraint(
                "fk_bridge_generation_slots_thread",
                "f",
                ("holder_thread_id",),
                referenced_table="editorial_threads",
                referenced_columns=("id",),
                confdeltype="r",
                confupdtype="a",
                confmatchtype="s",
            ),
            _constraint(
                "fk_bridge_generation_slots_turn",
                "f",
                ("holder_turn_id",),
                referenced_table="model_conversation_turns",
                referenced_columns=("id",),
                confdeltype="r",
                confupdtype="a",
                confmatchtype="s",
            ),
        ],
        "indexes": [
            {
                "index_name": "bridge_generation_slots_pkey",
                "table_name": "bridge_generation_slots",
                "indisunique": True,
                "indisprimary": True,
                "indisvalid": True,
                "indisready": True,
                "indnkeyatts": 1,
                "indnatts": 1,
                "amname": "btree",
                "sort_options": [0],
                "opclasses": ["int2_ops"],
                "collations": [None],
                "columns": ["singleton_key"],
                "predicate": None,
            }
        ],
        "functions": [
            {
                "pronargs": 0,
                "pronargdefaults": 0,
                "identity_arguments": "",
                "result_type": "trigger",
                "lanname": "plpgsql",
                "prokind": "f",
                "provolatile": "v",
                "proisstrict": False,
                "prosecdef": False,
                "proleakproof": False,
                "proparallel": "u",
                "proconfig": None,
                "prosrc": "\nBEGIN\n RAISE EXCEPTION "
                "'bridge_generation_slot_singleton_cannot_be_removed';\nEND;\n",
            }
        ],
        "triggers": [
            {
                "tgname": "trg_bridge_generation_slots_no_delete",
                "table_schema": "public",
                "table_name": "bridge_generation_slots",
                "tgtype": 11,
                "tgenabled": "O",
                "tgisinternal": False,
                "tgconstraint": 0,
                "tgnargs": 0,
                "update_column_count": 0,
                "has_no_condition": True,
                "argument_bytes": 0,
                "function_name": "reject_bridge_generation_slot_removal",
                "function_schema": "public",
            },
            {
                "tgname": "trg_bridge_generation_slots_no_truncate",
                "table_schema": "public",
                "table_name": "bridge_generation_slots",
                "tgtype": 34,
                "tgenabled": "O",
                "tgisinternal": False,
                "tgconstraint": 0,
                "tgnargs": 0,
                "update_column_count": 0,
                "has_no_condition": True,
                "argument_bytes": 0,
                "function_name": "reject_bridge_generation_slot_removal",
                "function_schema": "public",
            },
        ],
        "singleton_keys": [1],
    }


def test_bridge_generation_slot_preflight_accepts_postgres_deparsed_canonical_shape() -> None:
    assert _MIGRATION._schema_errors(_canonical_shape(), schema="public") == []


def test_bridge_generation_slot_preflight_reports_shape_mutations() -> None:
    shape = _canonical_shape()
    shape["columns"][4]["default_expression"] = "1"
    shape["constraints"][4]["definition"] = "CHECK ((lease_token)::smallint >= 0)"
    shape["triggers"][0]["tgenabled"] = "D"

    errors = _MIGRATION._schema_errors(shape, schema="public")

    assert any("column bridge_generation_slots.lease_token.default" in error for error in errors)
    assert any(
        "constraint ck_bridge_generation_slots_token.expression" in error for error in errors
    )
    assert any(
        "trigger trg_bridge_generation_slots_no_delete.tgenabled" in error for error in errors
    )


def test_bridge_generation_slot_preflight_reports_partial_schema_objects() -> None:
    shape = _canonical_shape()
    shape["relation"] = None
    shape["functions"] = []
    shape["triggers"] = []

    errors = _MIGRATION._schema_errors(shape, schema="public")

    assert any("relation bridge_generation_slots" in error for error in errors)
    assert any(
        "function reject_bridge_generation_slot_removal() count" in error for error in errors
    )
    assert any("triggers bridge_generation_slots" in error for error in errors)

"""Verify a Compose-migrated disposable PostgreSQL schema against test contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

backend_root = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(backend_root))

from tests.integration.test_migrations import (  # noqa: E402
    EXPECTED_FUNCTIONS,
    EXPECTED_TABLES,
    EXPECTED_TRIGGERS,
)


def query(container_id: str, statement: str) -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container_id,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "cti_app",
            "-d",
            "cti_app",
            "-A",
            "-t",
            "-F",
            "|",
        ],
        input=statement,
        capture_output=True,
        check=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def verify(container_id: str) -> None:
    revisions = query(
        container_id,
        "SELECT version_num FROM alembic_version ORDER BY version_num;",
    )
    if revisions != ["0029_note_anchor_identity"]:
        raise RuntimeError(f"Expected one 0029 head, got {revisions!r}")

    tables = set(
        query(
            container_id,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name;",
        )
    )
    if tables != EXPECTED_TABLES:
        raise RuntimeError(
            "Migrated table set differs from the integration contract: "
            f"missing={sorted(EXPECTED_TABLES - tables)!r}, "
            f"unexpected={sorted(tables - EXPECTED_TABLES)!r}"
        )

    functions = set(
        query(
            container_id,
            "SELECT p.proname FROM pg_proc AS p "
            "JOIN pg_namespace AS n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' ORDER BY p.proname;",
        )
    )
    if functions != EXPECTED_FUNCTIONS:
        raise RuntimeError(
            "Protection function set differs from the integration contract: "
            f"missing={sorted(EXPECTED_FUNCTIONS - functions)!r}, "
            f"unexpected={sorted(functions - EXPECTED_FUNCTIONS)!r}"
        )

    trigger_rows = query(
        container_id,
        "SELECT c.relname, t.tgname, p.proname "
        "FROM pg_trigger AS t "
        "JOIN pg_class AS c ON c.oid = t.tgrelid "
        "JOIN pg_proc AS p ON p.oid = t.tgfoid "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
        "ORDER BY c.relname, t.tgname;",
    )
    triggers = {}
    for row in trigger_rows:
        table_name, trigger_name, function_name = row.split("|")
        triggers[(table_name, trigger_name)] = function_name
    if triggers != EXPECTED_TRIGGERS:
        missing = EXPECTED_TRIGGERS.keys() - triggers.keys()
        unexpected = triggers.keys() - EXPECTED_TRIGGERS.keys()
        wrong_binding = {
            key: (EXPECTED_TRIGGERS[key], triggers[key])
            for key in EXPECTED_TRIGGERS.keys() & triggers.keys()
            if EXPECTED_TRIGGERS[key] != triggers[key]
        }
        raise RuntimeError(
            "Protection trigger set differs from the integration contract: "
            f"missing={sorted(missing)!r}, unexpected={sorted(unexpected)!r}, "
            f"wrong_binding={wrong_binding!r}"
        )


def main() -> int:
    container_id = os.environ.get("POSTGRES_CONTAINER")
    if not container_id:
        print("POSTGRES_CONTAINER is required", file=sys.stderr)
        return 2
    verify(container_id)
    print(
        "Schema verified: one 0029 head, "
        f"{len(EXPECTED_TABLES)} tables, {len(EXPECTED_FUNCTIONS)} functions, "
        f"{len(EXPECTED_TRIGGERS)} protection triggers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate the immutable 0001 DDL from its historical ORM-derived shape.

Provenance: Alembic environment and baseline at d7dd3a2d2dc3f7340019063bad38e6ddeb274cb9.
This one-shot generator extracts the historical retroactive metadata helper,
imports the historical baseline model registrations, and asks SQLAlchemy's
PostgreSQL DDL compiler to emit exactly the statements used by create_all.
It is a development artifact and is never imported by Alembic at runtime.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from sqlalchemy import MetaData, create_mock_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_ROOT = _REPO_ROOT / "backend"
_SOURCE_REVISION = "d7dd3a2d2dc3f7340019063bad38e6ddeb274cb9"
_ENV_PATH = "backend/migrations/env.py"
_BASELINE_PATH = "backend/migrations/versions/0001_baseline.py"
_ORM_PATH = "backend/src/cti_app/infrastructure/database/models"
_C2_TABLES = frozenset({"investigation_experiment_heads", "investigation_experiments"})
_DEFAULT_OUTPUT = _BACKEND_ROOT / "migrations/frozen/baseline_0001.py"


def _git_show(path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{_SOURCE_REVISION}:{path}"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _private_assignment(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Assign):
        return False
    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
    return bool(names) and all(name.startswith("_") for name in names)


def _historical_baseline_metadata() -> MetaData:
    result = subprocess.run(
        ["git", "diff", "--quiet", _SOURCE_REVISION, "--", _ORM_PATH],
        cwd=_REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ORM model sources must match the stated historical source revision"
        )

    sys.path.insert(0, str(_BACKEND_ROOT))
    environment = ast.parse(_git_show(_ENV_PATH), filename=_ENV_PATH)
    baseline = ast.parse(_git_show(_BASELINE_PATH), filename=_BASELINE_PATH)
    selected: list[ast.stmt] = []
    for node in environment.body:
        is_import = isinstance(node, (ast.Import, ast.ImportFrom)) and not (
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
        )
        is_metadata_factory = isinstance(node, ast.FunctionDef) and node.name == (
            "_metadata_for_frozen_baseline"
        )
        if is_import or _private_assignment(node) or is_metadata_factory:
            selected.append(node)
    for node in baseline.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and not (
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
        ):
            selected.append(node)

    namespace: dict[str, Any] = {}
    exec(  # noqa: S102 - execute only the fixed historical AST selected above
        compile(ast.Module(body=selected, type_ignores=[]), _ENV_PATH, "exec"),
        namespace,
    )
    base = namespace["Base"]
    namespace["target_metadata"] = base.metadata
    metadata_factory = namespace["_metadata_for_frozen_baseline"]
    return cast(MetaData, metadata_factory())


def _capture_ddl() -> tuple[tuple[str, ...], tuple[str, ...]]:
    metadata = _historical_baseline_metadata()
    tables = tuple(
        table for table in metadata.tables.values() if table.name not in _C2_TABLES
    )
    statements: list[str] = []
    mock_connection: Any

    def collect(statement: Any, *_args: Any, **_kwargs: Any) -> None:
        statements.append(str(statement.compile(dialect=mock_connection.dialect)))

    mock_connection = create_mock_engine("postgresql://", collect)
    metadata.create_all(bind=mock_connection, tables=tables, checkfirst=False)
    return tuple(table.name for table in tables), _normalize_unordered_ddl(statements)


def _split_table_clauses(statement: str) -> tuple[str, list[str], str] | None:
    start = statement.find("CREATE TABLE ")
    opening = statement.find("(", start)
    if start < 0 or opening < 0:
        return None

    depth = 0
    single_quoted = False
    double_quoted = False
    closing = -1
    index = opening
    while index < len(statement):
        character = statement[index]
        following = statement[index + 1] if index + 1 < len(statement) else ""
        if single_quoted:
            if character == "'" and following == "'":
                index += 2
                continue
            if character == "'":
                single_quoted = False
        elif double_quoted:
            if character == '"' and following == '"':
                index += 2
                continue
            if character == '"':
                double_quoted = False
        elif character == "'":
            single_quoted = True
        elif character == '"':
            double_quoted = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                closing = index
                break
        index += 1
    if closing < 0:
        raise ValueError(
            "could not find the end of a historical CREATE TABLE statement"
        )

    body = statement[opening + 1 : closing]
    clauses: list[str] = []
    clause_start = 0
    depth = 0
    single_quoted = False
    double_quoted = False
    index = 0
    while index < len(body):
        character = body[index]
        following = body[index + 1] if index + 1 < len(body) else ""
        if single_quoted:
            if character == "'" and following == "'":
                index += 2
                continue
            if character == "'":
                single_quoted = False
        elif double_quoted:
            if character == '"' and following == '"':
                index += 2
                continue
            if character == '"':
                double_quoted = False
        elif character == "'":
            single_quoted = True
        elif character == '"':
            double_quoted = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            clauses.append(body[clause_start:index].strip())
            clause_start = index + 1
        index += 1
    final_clause = body[clause_start:].strip()
    if final_clause:
        clauses.append(final_clause)
    return statement[: opening + 1], clauses, statement[closing:]


def _normalize_create_table(statement: str) -> str:
    split = _split_table_clauses(statement)
    if split is None:
        return statement
    prefix, clauses, suffix = split
    constraint_prefixes = (
        "CONSTRAINT ",
        "PRIMARY KEY",
        "FOREIGN KEY",
        "UNIQUE ",
        "CHECK ",
        "EXCLUDE ",
    )
    column_clauses = []
    constraints = []
    for clause in clauses:
        if clause.lstrip().upper().startswith(constraint_prefixes):
            constraints.append(clause)
        else:
            column_clauses.append(clause)
    constraints.sort(key=str.casefold)
    body = ", \n\t".join((*column_clauses, *constraints))
    return f"{prefix}\n\t{body}\n{suffix}"


def _statement_group(statement: str) -> str | None:
    normalized = statement.lstrip().upper()
    for prefix in ("CREATE INDEX ", "CREATE UNIQUE INDEX "):
        if normalized.startswith(prefix):
            return "index"
    if normalized.startswith("ALTER TABLE "):
        return "alter"
    if normalized.startswith("COMMENT ON "):
        return "comment"
    return None


def _normalize_unordered_ddl(statements: list[str]) -> tuple[str, ...]:
    result = [
        _normalize_create_table(statement)
        if "CREATE TABLE " in statement
        else statement
        for statement in statements
    ]
    index = 0
    while index < len(result):
        group = _statement_group(result[index])
        if group is None:
            index += 1
            continue
        stop = index + 1
        while stop < len(result) and _statement_group(result[stop]) == group:
            stop += 1
        result[index:stop] = sorted(result[index:stop], key=str.casefold)
        index = stop
    return tuple(result)


def _literal(value: str, width: int = 72) -> str:
    pieces = [value[offset : offset + width] for offset in range(0, len(value), width)]
    if len(pieces) == 1:
        return repr(pieces[0])
    return "(\n" + "\n".join(f"        {piece!r}" for piece in pieces) + "\n    )"


def _render_module(table_names: tuple[str, ...], statements: tuple[str, ...]) -> str:
    lines = [
        '"""Frozen DDL for the immutable pre-C2 Alembic 0001 baseline.',
        "",
        f"Source revision: {_SOURCE_REVISION}.",
        "IMMUTABLE — ne pas éditer; toute évolution = nouvelle révision.",
        "Generated by scripts/dev/freeze_0001_baseline.py from the historical",
        "retroactive baseline metadata and PostgreSQL SQLAlchemy DDL compiler.",
        '"""',
        "",
        "from typing import Final",
        "",
        "BASELINE_TABLE_NAMES: Final[tuple[str, ...]] = (",
    ]
    lines.extend(f"    {name!r}," for name in table_names)
    lines.extend((")", "", "BASELINE_CREATE_DDL: Final[tuple[str, ...]] = ("))
    lines.extend(f"    {_literal(statement)}," for statement in statements)
    lines.extend((")", ""))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()

    if os.environ.get("PYTHONHASHSEED") != "0":
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = "0"
        subprocess.run(
            [sys.executable, __file__, *sys.argv[1:]], env=environment, check=True
        )
        return 0

    table_names, statements = _capture_ddl()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(_render_module(table_names, statements), encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", str(args.output)],
        cwd=_REPO_ROOT,
        check=True,
    )
    print(
        f"generated {args.output}: {len(table_names)} tables, "
        f"{len(statements)} PostgreSQL DDL statements"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

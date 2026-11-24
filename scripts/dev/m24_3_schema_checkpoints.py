#!/usr/bin/env python3
"""Capture or compare disposable PostgreSQL schema checkpoints for M24.3."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_BACKEND_ROOT = _SCRIPT_REPO_ROOT / "backend"
sys.path.insert(0, str(_SCRIPT_BACKEND_ROOT))
from tests.support.schema_snapshot import capture_schema, diff_snapshots  # noqa: E402

ADMIN_DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres"
CHECKPOINTS = (
    "0001_baseline",
    "0002_c2_experiments",
    "0008b_version_length",
    "0018_editorial_enrichment_element_operations",
    "0026_workspace_note_annotations",
    "0029_note_anchor_identity",
)
_DATABASE_NAME = re.compile(r"m243_[0-9a-f]{32}\Z")


def _admin_url() -> URL:
    url = make_url(ADMIN_DSN)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.port != 55432
        or url.username != "postgres"
        or url.database != "postgres"
    ):
        raise RuntimeError("The disposable schema oracle is restricted to its local test PG DSN")
    return url


async def _verify_admin_target(engine: Any) -> None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT current_database(), current_user, inet_server_port()")
            )
        ).one()
    if row[0] != "postgres" or row[1] != "postgres":
        raise RuntimeError(
            "Refusing schema checkpoint operation outside the local test PG admin DB"
        )


async def _create_database(database_name: str) -> None:
    if not _DATABASE_NAME.fullmatch(database_name):
        raise ValueError("Refusing to create a database without a generated m243_ name")
    engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        await _verify_admin_target(engine)
        async with engine.connect() as connection:
            await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    finally:
        await engine.dispose()


async def _drop_database(database_name: str) -> None:
    if not _DATABASE_NAME.fullmatch(database_name):
        raise ValueError("Refusing to drop a database without a generated m243_ name")
    engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        await _verify_admin_target(engine)
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
    finally:
        await engine.dispose()


async def _capture_database(database_url: URL) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(capture_schema)
    finally:
        await engine.dispose()


def _alembic_config(repo_root: Path, database_url: URL) -> Config:
    backend_root = repo_root.resolve() / "backend"
    ini_path = backend_root / "alembic.ini"
    if not ini_path.is_file():
        raise FileNotFoundError(f"No backend Alembic config at {ini_path}")
    config = Config(str(ini_path))
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


def _load_snapshots(directory: Path) -> dict[str, dict[str, Any]]:
    if not directory.is_dir():
        raise FileNotFoundError(f"Snapshot directory does not exist: {directory}")
    snapshots: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Snapshot root must be a JSON object: {path}")
        snapshots[path.name] = payload
    if not snapshots:
        raise ValueError(f"No JSON snapshots found in {directory}")
    return snapshots


def _compare_directories(left_dir: Path, right_dir: Path) -> int:
    left = _load_snapshots(left_dir)
    right = _load_snapshots(right_dir)
    names = sorted(left.keys() | right.keys())
    divergent = False
    for name in names:
        if name not in left:
            print(f"{name}: missing from {left_dir}")
            divergent = True
        elif name not in right:
            print(f"{name}: missing from {right_dir}")
            divergent = True
        else:
            differences = diff_snapshots(left[name], right[name])
            if differences:
                divergent = True
                print(f"{name}: {len(differences)} difference(s)")
                for difference in differences:
                    print(f"  {difference}")
            else:
                print(f"{name}: identical")
    return 1 if divergent else 0


def _write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _capture_checkpoints(output_dir: Path, repo_root: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_files = {f"{revision}.json" for revision in CHECKPOINTS}
    existing_files = expected_files.intersection(path.name for path in output_dir.iterdir())
    if existing_files:
        raise FileExistsError(
            "Refusing to overwrite existing checkpoint(s): " + ", ".join(sorted(existing_files))
        )

    database_name = f"m243_{uuid4().hex}"
    admin_url = _admin_url()
    database_url = admin_url.set(database=database_name)
    created = False
    try:
        asyncio.run(_create_database(database_name))
        created = True
        config = _alembic_config(repo_root, database_url)
        for revision in CHECKPOINTS:
            command.upgrade(config, revision)
            snapshot = asyncio.run(_capture_database(database_url))
            output_path = output_dir / f"{revision}.json"
            _write_snapshot(output_path, snapshot)
            print(f"captured {revision}: {output_path}")
    finally:
        if created:
            asyncio.run(_drop_database(database_name))
            print(f"dropped disposable database {database_name}")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--out", type=Path, help="directory for new checkpoint JSON files")
    operation.add_argument(
        "--compare",
        nargs=2,
        type=Path,
        metavar=("DIR_A", "DIR_B"),
        help="compare matching JSON snapshot files; exit 1 on divergence",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_SCRIPT_REPO_ROOT,
        help="checkout whose backend Alembic config and migrations are used",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.compare is not None:
        return _compare_directories(args.compare[0], args.compare[1])
    _capture_checkpoints(args.out, args.repo_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

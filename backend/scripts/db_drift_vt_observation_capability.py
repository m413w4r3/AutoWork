from __future__ import annotations

import argparse
import asyncio
import re
from collections.abc import Sequence
from typing import cast

from sqlalchemy import CheckConstraint, Table, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from cti_app.domain.virustotal import VirusTotalCapability
from cti_app.infrastructure.database.models.core import VirusTotalObservationRow

_CONSTRAINT_NAME = "ck_vt_observation_capability"
_TABLE_NAME = "virustotal_observations"
_SQL_LITERAL = re.compile(r"'((?:''|[^'])*)'")


def _sql_literals(definition: str) -> set[str]:
    return {match.replace("''", "'") for match in _SQL_LITERAL.findall(definition)}


def _has_supported_shape(definition: str, value_count: int) -> bool:
    normalized = _SQL_LITERAL.sub("?", definition)
    normalized = re.sub(
        r"::\s*(?:character varying|text)(?:\[\])?",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r'[\s()"]', "", normalized).lower()
    placeholders = ",".join("?" for _ in range(value_count))
    return normalized in {
        f"capabilityin{placeholders}",
        f"checkcapabilityin{placeholders}",
        f"capability=anyarray[{placeholders}]",
        f"checkcapability=anyarray[{placeholders}]",
    }


def _expected_values() -> tuple[set[str], str]:
    enum_values = {capability.value for capability in VirusTotalCapability}
    table = cast(Table, VirusTotalObservationRow.__table__)
    constraint: CheckConstraint | None = next(
        (
            item
            for item in table.constraints
            if isinstance(item, CheckConstraint) and item.name == _CONSTRAINT_NAME
        ),
        None,
    )
    if constraint is None:
        raise RuntimeError(f"Le modèle ne déclare pas {_CONSTRAINT_NAME}.")
    model_definition = str(constraint.sqltext)
    if _sql_literals(model_definition) != enum_values or not _has_supported_shape(
        model_definition, len(enum_values)
    ):
        raise RuntimeError(f"La CHECK du modèle diverge de l'enum {VirusTotalCapability.__name__}.")
    return enum_values, model_definition


async def _database_definition(dsn: str) -> str | None:
    url = make_url(dsn)
    if url.drivername not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ValueError("Le DSN doit cibler PostgreSQL.")
    if url.drivername != "postgresql+asyncpg":
        url = url.set(drivername="postgresql+asyncpg")

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            result = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(constraint_row.oid) "
                    "FROM pg_constraint AS constraint_row "
                    "WHERE constraint_row.conrelid = to_regclass(:table_name) "
                    "AND constraint_row.conname = :constraint_name "
                    "AND constraint_row.contype = 'c'"
                ),
                {"table_name": _TABLE_NAME, "constraint_name": _CONSTRAINT_NAME},
            )
            return result if isinstance(result, str) else None
    finally:
        await engine.dispose()


async def _check(dsn: str) -> int:
    expected, model_definition = _expected_values()
    database_definition = await _database_definition(dsn)
    if database_definition is None:
        print(f"DRIFT: {_TABLE_NAME}.{_CONSTRAINT_NAME} absente.")
        print(f"Modèle: {model_definition}")
        return 1

    actual = _sql_literals(database_definition)
    shape_matches = _has_supported_shape(database_definition, len(expected))
    if actual == expected and shape_matches:
        print(
            f"OK: {_TABLE_NAME}.{_CONSTRAINT_NAME} correspond au modèle "
            f"({len(expected)} capacités)."
        )
        return 0

    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    print(f"DRIFT: {_TABLE_NAME}.{_CONSTRAINT_NAME} diffère du modèle/enums.")
    print(f"Capacités manquantes: {', '.join(missing) if missing else '(aucune)'}")
    print(f"Capacités inattendues: {', '.join(unexpected) if unexpected else '(aucune)'}")
    print(f"Structure CHECK reconnue: {'oui' if shape_matches else 'non'}")
    print(f"Définition PostgreSQL: {database_definition}")
    print(f"Définition modèle: {model_definition}")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare en lecture seule ck_vt_observation_capability avec le modèle et "
            "VirusTotalCapability. Le DSN est fourni en argument, jamais lu depuis .env."
        )
    )
    parser.add_argument("dsn", help="DSN SQLAlchemy PostgreSQL de la base à contrôler")
    arguments = parser.parse_args(argv)
    try:
        return asyncio.run(_check(arguments.dsn))
    except Exception as exc:
        # Driver exceptions can embed the full DSN; expose only their class.
        print(f"ERREUR lecture PostgreSQL: {type(exc).__name__}.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

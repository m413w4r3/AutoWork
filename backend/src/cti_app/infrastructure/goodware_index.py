from __future__ import annotations

import asyncio
import fcntl
import hashlib
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, cast

from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.errors import BlobIntegrityError
from cti_app.domain.goodware_index import (
    INDEX_FORMAT_VERSION,
    KEY_VERSION,
    NON_DISCRIMINANT_PATTERN_VERSION,
    NORMALIZATION_VERSION,
    SCHEMA_VERSION,
    SOURCE_FORMAT,
    GoodwareImportError,
    GoodwareMeasurementError,
    goodware_lookup_key,
)

if TYPE_CHECKING:
    from cti_app.application.blob_storage import BlobStore

GOODWARE_CACHE_ROOT = Path("/var/cache/autowork/goodware")

_FEATURES_SQL = """CREATE TABLE features (
    feature_key BLOB PRIMARY KEY CHECK(length(feature_key) = 32),
    occurrence_count INTEGER NOT NULL CHECK(occurrence_count > 0)
) WITHOUT ROWID"""
_METADATA_SQL = """CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID"""
_METADATA_KEYS = (
    "baseline_fingerprint_sha256",
    "index_format_version",
    "key_version",
    "normalization_version",
    "occurrence_sum",
    "pattern_version",
    "record_count",
    "schema_version",
    "source_format",
    "source_set_sha256",
)
_CACHE_BATCH_SIZE = 900


def _normalized_sql(value: str) -> str:
    return " ".join(value.split())


def _check_table_schema(
    connection: sqlite3.Connection,
    table: str,
    expected_sql: str,
    expected_columns: list[tuple[str, str, int, int]],
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise GoodwareImportError(f"missing SQLite table: {table}")
    if _normalized_sql(row[0]) != _normalized_sql(expected_sql):
        raise GoodwareImportError(f"SQLite schema mismatch: {table}")
    columns = [
        (str(item[1]), str(item[2]), int(item[3]), int(item[5]))
        for item in connection.execute(f"PRAGMA table_info({table})")
    ]
    if columns != expected_columns:
        raise GoodwareImportError(f"SQLite columns mismatch: {table}")
    indexes = list(connection.execute(f"PRAGMA index_list({table})"))
    if len(indexes) != 1 or indexes[0][3] != "pk" or indexes[0][4] != 0:
        raise GoodwareImportError(f"unexpected SQLite index on {table}")


def expected_metadata(manifest: Mapping[str, object], *, pattern_version: str) -> dict[str, str]:
    return {
        "baseline_fingerprint_sha256": cast(str, manifest["baseline_fingerprint_sha256"]),
        "index_format_version": INDEX_FORMAT_VERSION,
        "key_version": KEY_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "occurrence_sum": str(manifest["occurrence_sum"]),
        "pattern_version": pattern_version,
        "record_count": str(manifest["record_count"]),
        "schema_version": SCHEMA_VERSION,
        "source_format": SOURCE_FORMAT,
        "source_set_sha256": cast(str, manifest["source_set_sha256"]),
    }


def verify_sqlite_index(
    index_path: Path,
    manifest: Mapping[str, object],
    *,
    pattern_version: str = NON_DISCRIMINANT_PATTERN_VERSION,
) -> None:
    for suffix in ("-wal", "-shm"):
        if index_path.with_name(index_path.name + suffix).exists():
            raise GoodwareImportError(f"SQLite index has an unexpected {suffix} sidecar")
    try:
        connection = sqlite3.connect(f"{index_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise GoodwareImportError("cannot open SQLite index read-only") from exc
    try:
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise GoodwareImportError("SQLite index is not query-only")
        if connection.execute("PRAGMA writable_schema").fetchone()[0] != 0:
            raise GoodwareImportError("SQLite writable_schema is enabled")
        databases = [row[1] for row in connection.execute("PRAGMA database_list")]
        if databases != ["main"]:
            raise GoodwareImportError("SQLite index has unexpected attached databases")
        objects = list(
            connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        if objects != [("table", "features"), ("table", "metadata")]:
            raise GoodwareImportError("SQLite index has unexpected schema objects")
        _check_table_schema(
            connection,
            "features",
            _FEATURES_SQL,
            [("feature_key", "BLOB", 1, 1), ("occurrence_count", "INTEGER", 1, 0)],
        )
        _check_table_schema(
            connection,
            "metadata",
            _METADATA_SQL,
            [("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)],
        )
        metadata_rows = list(connection.execute("SELECT key, value FROM metadata"))
        expected = expected_metadata(manifest, pattern_version=pattern_version)
        if len(metadata_rows) != len(_METADATA_KEYS) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in metadata_rows
        ):
            raise GoodwareImportError("SQLite metadata is malformed")
        if dict(metadata_rows) != expected:
            raise GoodwareImportError("SQLite metadata does not match manifest")
    except GoodwareImportError:
        raise
    except (IndexError, TypeError, ValueError, sqlite3.Error) as exc:
        raise GoodwareImportError("invalid SQLite index") from exc
    finally:
        connection.close()


class GoodwareSQLiteReader:
    """Database-free reader for an already prepared, verified SQLite file."""

    def __init__(self, index_path: Path) -> None:
        self._index_path = index_path

    def lookup(self, feature_kind: str, normalized_value: str) -> int | None:
        key = goodware_lookup_key(feature_kind, normalized_value)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT occurrence_count FROM features WHERE feature_key = ?", (key,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise GoodwareMeasurementError("cannot query read-only SQLite index") from exc
        finally:
            connection.close()
        return None if row is None else int(row[0])

    def lookup_batch(self, features: Sequence[tuple[str, str]]) -> dict[tuple[str, str], int]:
        keys: dict[bytes, tuple[str, str]] = {}
        for feature_kind, normalized_value in features:
            keys[goodware_lookup_key(feature_kind, normalized_value)] = (
                feature_kind,
                normalized_value,
            )
        if not keys:
            return {}
        connection = self._connect()
        try:
            counts: dict[bytes, int] = {}
            key_values = tuple(keys)
            for offset in range(0, len(key_values), _CACHE_BATCH_SIZE):
                chunk = key_values[offset : offset + _CACHE_BATCH_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT feature_key, occurrence_count FROM features "
                    f"WHERE feature_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                counts.update({cast(bytes, key): int(count) for key, count in rows})
        except sqlite3.Error as exc:
            raise GoodwareMeasurementError("cannot query read-only SQLite index") from exc
        finally:
            connection.close()
        return {keys[key]: count for key, count in counts.items()}

    def lookup_values(
        self, feature_kind: str, normalized_values: Sequence[str]
    ) -> Mapping[str, int]:
        return {
            value: count
            for (kind, value), count in self.lookup_batch(
                [(feature_kind, value) for value in normalized_values]
            ).items()
            if kind == feature_kind
        }

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(f"{self._index_path.resolve().as_uri()}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                connection.close()
                raise GoodwareMeasurementError("SQLite index is not query-only")
            return connection
        except GoodwareMeasurementError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise GoodwareMeasurementError("cannot open read-only SQLite index") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_cached_file(path: Path, descriptor: BlobDescriptor) -> None:
    if not path.is_file() or path.is_symlink():
        raise BlobIntegrityError(f"Cached goodware index is not a regular file: {path}")
    try:
        size = path.stat().st_size
        digest = sha256_file(path)
    except OSError as exc:
        raise BlobIntegrityError(f"Cannot read cached goodware index: {path}") from exc
    if size != descriptor.size or digest != descriptor.sha256:
        raise BlobIntegrityError(
            f"Cached goodware index does not match descriptor for {descriptor.sha256}"
        )


def _acquire_cache_lock(path: Path) -> BinaryIO:
    handle = path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_cache_lock(handle: BinaryIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


async def prepare_cached_index(
    store: BlobStore,
    descriptor: BlobDescriptor,
    cache_root: Path,
    *,
    verify: Callable[[Path, BlobDescriptor], None] = verify_cached_file,
) -> Path:
    await asyncio.to_thread(cache_root.mkdir, parents=True, exist_ok=True)
    destination = cache_root / f"{descriptor.sha256}.sqlite3"
    lock_path = cache_root / f".{descriptor.sha256}.lock"
    lock_handle = await asyncio.to_thread(_acquire_cache_lock, lock_path)
    try:
        if await asyncio.to_thread(destination.is_symlink):
            raise BlobIntegrityError("Refusing to use a symbolic link as a goodware cache")
        if await asyncio.to_thread(destination.exists):
            await asyncio.to_thread(verify, destination, descriptor)
        else:
            await store.materialize(descriptor, destination)
            await asyncio.to_thread(verify, destination, descriptor)
            await asyncio.to_thread(_make_read_only, destination)
        return destination
    finally:
        await asyncio.to_thread(_release_cache_lock, lock_handle)


def _make_read_only(path: Path) -> None:
    path.chmod(path.stat().st_mode & ~0o222)


# Compatibility for callers that used the original application-local helpers.
_FEATURES_SQL = _FEATURES_SQL
_METADATA_SQL = _METADATA_SQL
_expected_metadata = expected_metadata
_verify_sqlite_index = verify_sqlite_index
_verify_cached_file = verify_cached_file

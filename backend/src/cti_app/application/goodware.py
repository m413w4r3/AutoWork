from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import get_settings
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.errors import BlobIntegrityError
from cti_app.domain.goodware import (
    GoodwareBaseline,
    GoodwareBaselineError,
    GoodwareIndexArtifact,
    GoodwareSource,
)

SCHEMA_VERSION = "autowork-goodware-index-v2"
NORMALIZATION_VERSION = "autowork-goodware-normalization-v2"
KEY_VERSION = "autowork-goodware-key-v1"
INDEX_FORMAT_VERSION = "autowork-goodware-index-v2"
SOURCE_FORMAT = "yargen-gzip-json-counter-v1"
NON_DISCRIMINANT_PATTERN_VERSION = "non-discriminant-patterns-v1"
INDEX_FILENAME = "goodware-index.sqlite3"
MANIFEST_FILENAME = "manifest.json"
GOODWARE_CACHE_ROOT = Path("/var/cache/autowork/goodware")

SUPPORTED_FEATURE_KINDS = (
    "string",
    "opcode_fragment16",
    "imphash",
    "export",
)
_SUPPORTED_FEATURE_KIND_SET = frozenset(SUPPORTED_FEATURE_KINDS)
_SOURCE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("good-strings", "string"),
    ("good-opcodes", "opcode_fragment16"),
    ("good-imphashes", "imphash"),
    ("good-imphash", "imphash"),
    ("good-exports", "export"),
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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
_MAX_MANIFEST_BYTES = 1024 * 1024


class GoodwareImportError(GoodwareBaselineError):
    pass


class GoodwareMeasurementError(GoodwareBaselineError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def goodware_lookup_key(feature_kind: str, normalized_value: str) -> bytes:
    """Return the frozen key for an already-normalized runtime feature."""
    if not isinstance(feature_kind, str) or feature_kind not in _SUPPORTED_FEATURE_KIND_SET:
        raise GoodwareMeasurementError(f"unsupported feature kind: {feature_kind}")
    if not isinstance(normalized_value, str):
        raise GoodwareMeasurementError("normalized feature value must be a string")
    try:
        kind_bytes = feature_kind.encode("ascii")
    except UnicodeEncodeError as exc:
        raise GoodwareMeasurementError("feature kind must be ASCII") from exc
    return hashlib.sha256(
        KEY_VERSION.encode("ascii")
        + b"\0"
        + kind_bytes
        + b"\0"
        + normalized_value.encode("utf-8")
    ).digest()


lookup_key = goodware_lookup_key
canonical_lookup_key = goodware_lookup_key


def baseline_fingerprint_sha256(
    source_set_sha256: str,
    *,
    pattern_version: str = NON_DISCRIMINANT_PATTERN_VERSION,
) -> str:
    value = {
        "normalization_version": NORMALIZATION_VERSION,
        "pattern_version": pattern_version,
        "source_set_sha256": source_set_sha256,
    }
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise GoodwareImportError(f"manifest {field} must be a lowercase SHA-256")
    return value


def _source_kind(filename: str) -> str | None:
    lowered = filename.lower()
    if not lowered.endswith(".db"):
        return None
    for prefix, kind in _SOURCE_PREFIXES:
        if lowered.startswith(prefix):
            return kind
    return None


def _validate_source_filename(value: object) -> str:
    if not isinstance(value, str):
        raise GoodwareImportError("manifest source filename must be a string")
    path = Path(value)
    if path.is_absolute() or path.name != value or ".." in path.parts:
        raise GoodwareImportError(f"unsafe source filename: {value!r}")
    return value


def _source_set_sha256(sources: Sequence[Mapping[str, object]]) -> str:
    stable = [
        {
            "filename": source["filename"],
            "feature_kind": source["feature_kind"],
            "sha256": source["sha256"],
            "size": source["size"],
        }
        for source in sources
    ]
    return hashlib.sha256(_canonical_json(stable).encode("utf-8")).hexdigest()


def _non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GoodwareImportError(f"manifest {field} must be a non-negative integer")
    return value


def _validate_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise GoodwareImportError("manifest.json root must be an object")
    expected_keys = {
        "schema_version",
        "source_format",
        "source_set_sha256",
        "normalization_version",
        "key_version",
        "index_format_version",
        "baseline_fingerprint_sha256",
        "record_count",
        "occurrence_sum",
        "index_sha256",
        "index_size",
        "sources",
    }
    if set(manifest) != expected_keys:
        raise GoodwareImportError("manifest has unexpected or missing fields")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise GoodwareImportError("unsupported index schema version")
    if manifest["source_format"] != SOURCE_FORMAT:
        raise GoodwareImportError("unsupported source format")
    if manifest["normalization_version"] != NORMALIZATION_VERSION:
        raise GoodwareImportError("unsupported normalization version")
    if manifest["key_version"] != KEY_VERSION:
        raise GoodwareImportError("unsupported lookup-key version")
    if manifest["index_format_version"] != INDEX_FORMAT_VERSION:
        raise GoodwareImportError("unsupported index format version")

    source_set = _require_sha256(manifest["source_set_sha256"], "source_set_sha256")
    baseline = _require_sha256(
        manifest["baseline_fingerprint_sha256"], "baseline_fingerprint_sha256"
    )
    if baseline != baseline_fingerprint_sha256(source_set):
        raise GoodwareImportError("baseline_fingerprint_sha256 does not match manifest")
    _require_sha256(manifest["index_sha256"], "index_sha256")
    for field in ("record_count", "occurrence_sum", "index_size"):
        _non_negative_integer(manifest[field], field)
    if manifest["index_size"] == 0:
        raise GoodwareImportError("manifest index_size must be positive")

    sources_value = manifest["sources"]
    if not isinstance(sources_value, list) or not sources_value:
        raise GoodwareImportError("manifest sources must be a non-empty list")
    source_keys = {
        "filename",
        "feature_kind",
        "sha256",
        "size",
        "entry_count",
        "occurrence_sum",
    }
    sources: list[dict[str, object]] = []
    filenames: list[str] = []
    for source_value in sources_value:
        if not isinstance(source_value, dict) or set(source_value) != source_keys:
            raise GoodwareImportError("invalid source manifest entry")
        source = cast(dict[str, object], source_value)
        filename = _validate_source_filename(source["filename"])
        feature_kind = source["feature_kind"]
        if (
            not isinstance(feature_kind, str)
            or feature_kind not in _SUPPORTED_FEATURE_KIND_SET
            or _source_kind(filename) != feature_kind
        ):
            raise GoodwareImportError("invalid source feature kind")
        _require_sha256(source["sha256"], f"source {filename} sha256")
        for field in ("size", "entry_count", "occurrence_sum"):
            _non_negative_integer(source[field], f"source {filename} {field}")
        sources.append(source)
        filenames.append(filename)
    if filenames != sorted(filenames) or len(set(filenames)) != len(filenames):
        raise GoodwareImportError("manifest sources are not sorted and unique")
    if _source_set_sha256(sources) != source_set:
        raise GoodwareImportError("source_set_sha256 does not match sources")
    return cast(dict[str, Any], manifest)


def _read_manifest(path: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if max_bytes is not None and len(raw) > max_bytes:
            raise GoodwareImportError("manifest.json exceeds the read limit")
        manifest = json.loads(raw.decode("utf-8"))
    except GoodwareImportError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoodwareImportError("invalid or missing manifest.json") from exc
    try:
        canonical = (_canonical_json(manifest) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GoodwareImportError("manifest.json is not canonical JSON") from exc
    if raw != canonical:
        raise GoodwareImportError("manifest.json is not canonical JSON")
    return _validate_manifest(manifest)


def _verify_sources(source_dir: Path, manifest: Mapping[str, object]) -> None:
    if not source_dir.is_dir():
        raise GoodwareImportError(f"source directory does not exist: {source_dir}")
    paths = sorted(
        (
            path
            for path in source_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".db"
        ),
        key=lambda path: path.name,
    )
    actual: list[dict[str, object]] = []
    for path in paths:
        kind = _source_kind(path.name)
        if kind is None:
            raise GoodwareImportError(f"unsupported source database: {path.name}")
        actual.append(
            {
                "filename": path.name,
                "feature_kind": kind,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    expected = [
        {
            "filename": source["filename"],
            "feature_kind": source["feature_kind"],
            "sha256": source["sha256"],
            "size": source["size"],
        }
        for source in cast(list[dict[str, object]], manifest["sources"])
    ]
    if actual != expected:
        raise GoodwareImportError("source directory does not match source_set_sha256")
    if _source_set_sha256(actual) != manifest["source_set_sha256"]:
        raise GoodwareImportError("source directory source-set hash mismatch")


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


def _expected_metadata(
    manifest: Mapping[str, object], *, pattern_version: str
) -> dict[str, str]:
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


def _verify_sqlite_index(
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
        expected = _expected_metadata(manifest, pattern_version=pattern_version)
        if len(metadata_rows) != len(_METADATA_KEYS) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata_rows
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


def _verify_index_file(index_path: Path, manifest: Mapping[str, object]) -> None:
    try:
        size = index_path.stat().st_size
    except OSError as exc:
        raise GoodwareImportError(f"missing {INDEX_FILENAME}") from exc
    if size != manifest["index_size"]:
        raise GoodwareImportError("SQLite index size does not match manifest")
    if _sha256_file(index_path) != manifest["index_sha256"]:
        raise GoodwareImportError("SQLite index SHA-256 does not match manifest")


def _validate_artifact(artifact_dir: Path, source_dir: Path) -> dict[str, Any]:
    if not artifact_dir.is_dir():
        raise GoodwareImportError(f"artifact directory does not exist: {artifact_dir}")
    manifest = _read_manifest(artifact_dir / MANIFEST_FILENAME)
    index_path = artifact_dir / INDEX_FILENAME
    if not index_path.is_file():
        raise GoodwareImportError(f"missing {INDEX_FILENAME}")
    _verify_index_file(index_path, manifest)
    _verify_sources(source_dir, manifest)
    _verify_sqlite_index(index_path, manifest)
    return manifest


class GoodwareService:
    def __init__(self, blobs: BlobCatalogService, uow_factory: UnitOfWorkFactory) -> None:
        self._blobs, self._uow_factory = blobs, uow_factory

    async def import_index(self, artifact_dir: Path, source_dir: Path) -> GoodwareBaseline:
        """Validate and persist one immutable v2 goodware index."""
        manifest = _validate_artifact(artifact_dir, source_dir)
        fingerprint = cast(str, manifest["baseline_fingerprint_sha256"])
        async with self._uow_factory() as uow:
            existing = await uow.goodware_baselines.get_by_baseline_fingerprint_sha256(
                fingerprint
            )
            if existing is not None:
                return existing

        sources: list[GoodwareSource] = []
        for source_value in cast(list[dict[str, object]], manifest["sources"]):
            filename = cast(str, source_value["filename"])
            with (source_dir / filename).open("rb") as handle:
                blob = await self._blobs.ingest(
                    handle,
                    logical_bucket="goodware-baselines",
                    mime_type="application/octet-stream",
                )
            sources.append(
                GoodwareSource(
                    filename=filename,
                    feature_kind=cast(str, source_value["feature_kind"]),
                    sha256=cast(str, source_value["sha256"]),
                    size=cast(int, source_value["size"]),
                    blob_id=blob.id,
                )
            )

        with (artifact_dir / INDEX_FILENAME).open("rb") as handle:
            index_blob = await self._blobs.ingest(
                handle,
                logical_bucket="goodware-indexes",
                mime_type="application/vnd.sqlite3",
            )
        with (artifact_dir / MANIFEST_FILENAME).open("rb") as handle:
            manifest_blob = await self._blobs.ingest(
                handle,
                logical_bucket="goodware-index-manifests",
                mime_type="application/json",
            )

        baseline = GoodwareBaseline(
            id=uuid4(),
            baseline_fingerprint_sha256=fingerprint,
            source_set_sha256=cast(str, manifest["source_set_sha256"]),
            normalization_version=cast(str, manifest["normalization_version"]),
            record_count=cast(int, manifest["record_count"]),
            occurrence_sum=cast(int, manifest["occurrence_sum"]),
            pattern_version=NON_DISCRIMINANT_PATTERN_VERSION,
            sources=tuple(sources),
        )
        index_artifact = GoodwareIndexArtifact(
            id=uuid4(),
            baseline_id=baseline.id,
            schema_version=cast(str, manifest["schema_version"]),
            key_version=cast(str, manifest["key_version"]),
            index_format_version=cast(str, manifest["index_format_version"]),
            index_blob_id=index_blob.id,
            manifest_blob_id=manifest_blob.id,
        )

        async with self._uow_factory() as uow:
            inserted = await uow.goodware_baselines.add_if_absent(baseline)
            if not inserted:
                existing = await uow.goodware_baselines.get_by_baseline_fingerprint_sha256(
                    fingerprint
                )
                if existing is None:
                    raise RuntimeError("goodware baseline conflict without row")
                return existing
            await uow.goodware_baselines.add_sources(baseline.id, baseline.sources)
            await uow.goodware_baselines.add_index_artifact(index_artifact)
            await uow.commit()
            return baseline

    async def bind(self, investigation_id: UUID, baseline_id: UUID) -> None:
        async with self._uow_factory() as uow:
            inserted = await uow.investigation_goodware_baselines.add_if_absent(
                investigation_id, baseline_id
            )
            if not inserted:
                current = await uow.investigation_goodware_baselines.get(investigation_id)
                if current != baseline_id:
                    raise ValueError("investigation is already bound to another goodware baseline")
            else:
                await uow.commit()


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

    def lookup_batch(
        self, features: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], int]:
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
            connection = sqlite3.connect(
                f"{self._index_path.resolve().as_uri()}?mode=ro", uri=True
            )
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                connection.close()
                raise GoodwareMeasurementError("SQLite index is not query-only")
            return connection
        except GoodwareMeasurementError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise GoodwareMeasurementError("cannot open read-only SQLite index") from exc


class GoodwareMeasurementService:
    def __init__(
        self,
        store: BlobStore,
        uow_factory: UnitOfWorkFactory,
        *,
        cache_root: Path | None = None,
    ) -> None:
        self._store = store
        self._uow_factory = uow_factory
        self._cache_root = (
            cache_root if cache_root is not None else get_settings().goodware_cache_root
        )

    async def get_feature_occurrence(
        self,
        baseline: UUID | str,
        feature_kind: str,
        normalized_value: str,
    ) -> int | None:
        index_path = await self._prepare_index(baseline)
        return await asyncio.to_thread(
            GoodwareSQLiteReader(index_path).lookup,
            feature_kind,
            normalized_value,
        )

    async def get_feature_occurrences(
        self,
        baseline: UUID | str,
        feature_kind: str,
        normalized_values: Sequence[str],
    ) -> Mapping[str, int]:
        values = tuple(dict.fromkeys(normalized_values))
        if not values:
            return {}
        index_path = await self._prepare_index(baseline)
        return await asyncio.to_thread(
            GoodwareSQLiteReader(index_path).lookup_values,
            feature_kind,
            values,
        )

    async def lookup(
        self,
        baseline: UUID | str,
        feature_kind: str,
        normalized_value: str,
    ) -> int | None:
        return await self.get_feature_occurrence(baseline, feature_kind, normalized_value)

    async def lookup_batch(
        self, baseline: UUID | str, features: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], int]:
        if not features:
            return {}
        index_path = await self._prepare_index(baseline)
        return await asyncio.to_thread(GoodwareSQLiteReader(index_path).lookup_batch, features)

    async def _prepare_index(self, baseline_ref: UUID | str) -> Path:
        baseline, artifact, index_descriptor, manifest_descriptor = await self._resolve(
            baseline_ref
        )
        manifest_bytes = await self._store.read(
            manifest_descriptor, max_bytes=_MAX_MANIFEST_BYTES
        )
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoodwareMeasurementError("invalid stored goodware manifest") from exc
        try:
            canonical = (_canonical_json(manifest) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise GoodwareMeasurementError(
                "stored goodware manifest is not canonical JSON"
            ) from exc
        if manifest_bytes != canonical:
            raise GoodwareMeasurementError("stored goodware manifest is not canonical JSON")
        manifest = _validate_manifest(manifest)
        self._validate_runtime_metadata(
            baseline, artifact, index_descriptor, manifest_descriptor, manifest
        )
        index_path = await self._prepare_cached_index(index_descriptor)
        await asyncio.to_thread(
            _verify_sqlite_index,
            index_path,
            manifest,
            pattern_version=baseline.pattern_version,
        )
        return index_path

    async def _resolve(
        self, baseline_ref: UUID | str
    ) -> tuple[GoodwareBaseline, GoodwareIndexArtifact, BlobDescriptor, BlobDescriptor]:
        async with self._uow_factory() as uow:
            if isinstance(baseline_ref, UUID):
                baseline = await uow.goodware_baselines.get(baseline_ref)
            else:
                baseline = await uow.goodware_baselines.get_by_baseline_fingerprint_sha256(
                    baseline_ref
                )
            if baseline is None:
                raise GoodwareMeasurementError("goodware baseline does not exist")
            artifact = await uow.goodware_baselines.get_index_artifact(
                baseline.id,
                index_format_version=INDEX_FORMAT_VERSION,
                key_version=KEY_VERSION,
            )
            if artifact is None:
                raise GoodwareMeasurementError("goodware index artifact does not exist")
            index_blob = await uow.blobs.get(artifact.index_blob_id)
            manifest_blob = await uow.blobs.get(artifact.manifest_blob_id)
            if index_blob is None or manifest_blob is None:
                raise GoodwareMeasurementError("goodware index blob metadata is missing")
            return baseline, artifact, index_blob.descriptor, manifest_blob.descriptor

    @staticmethod
    def _validate_runtime_metadata(
        baseline: GoodwareBaseline,
        artifact: GoodwareIndexArtifact,
        index_descriptor: BlobDescriptor,
        manifest_descriptor: BlobDescriptor,
        manifest: Mapping[str, object],
    ) -> None:
        if artifact.baseline_id != baseline.id:
            raise GoodwareMeasurementError("goodware index artifact is bound to another baseline")
        expected = {
            "baseline_fingerprint_sha256": baseline.baseline_fingerprint_sha256,
            "source_set_sha256": baseline.source_set_sha256,
            "normalization_version": baseline.normalization_version,
            "record_count": baseline.record_count,
            "occurrence_sum": baseline.occurrence_sum,
            "schema_version": artifact.schema_version,
            "key_version": artifact.key_version,
            "index_format_version": artifact.index_format_version,
            "index_sha256": index_descriptor.sha256,
            "index_size": index_descriptor.size,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise GoodwareMeasurementError("goodware manifest metadata does not match PostgreSQL")
        if manifest_descriptor.logical_bucket != "goodware-index-manifests":
            raise GoodwareMeasurementError("goodware manifest has an unexpected blob bucket")
        if index_descriptor.logical_bucket != "goodware-indexes":
            raise GoodwareMeasurementError("goodware index has an unexpected blob bucket")

    async def _prepare_cached_index(self, descriptor: BlobDescriptor) -> Path:
        self._cache_root.mkdir(parents=True, exist_ok=True)
        destination = self._cache_root / f"{descriptor.sha256}.sqlite3"
        lock_path = self._cache_root / f".{descriptor.sha256}.lock"
        lock_handle = await asyncio.to_thread(_acquire_cache_lock, lock_path)
        try:
            if destination.is_symlink():
                raise BlobIntegrityError("Refusing to use a symbolic link as a goodware cache")
            if destination.exists():
                _verify_cached_file(destination, descriptor)
            else:
                await self._store.materialize(descriptor, destination)
                _verify_cached_file(destination, descriptor)
                destination.chmod(destination.stat().st_mode & ~0o222)
            return destination
        finally:
            await asyncio.to_thread(_release_cache_lock, lock_handle)


def _verify_cached_file(path: Path, descriptor: BlobDescriptor) -> None:
    if not path.is_file() or path.is_symlink():
        raise BlobIntegrityError(f"Cached goodware index is not a regular file: {path}")
    try:
        size = path.stat().st_size
        digest = _sha256_file(path)
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

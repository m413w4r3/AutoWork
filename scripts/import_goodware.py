#!/usr/bin/env python3
"""Build and verify offline AutoWork goodware artifacts.

The legacy ``build`` command creates the v1 JSONL stage consumed by the
current application code.  The ``build-index`` command creates the v2 SQLite
artifact.  Both paths are deliberately offline and database-independent.
"""

from __future__ import annotations

import argparse
import codecs
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

V1_SCHEMA_VERSION = "autowork-goodware-stage-v1"
SOURCE_FORMAT = "yargen-gzip-json-counter-v1"
RECORDS_FILENAME = "records.jsonl"
MANIFEST_FILENAME = "manifest.json"

SCHEMA_VERSION = "autowork-goodware-index-v2"
NORMALIZATION_VERSION = "autowork-goodware-normalization-v2"
KEY_VERSION = "autowork-goodware-key-v1"
INDEX_FORMAT_VERSION = "autowork-goodware-index-v2"
INDEX_FILENAME = "goodware-index.sqlite3"
NON_DISCRIMINANT_PATTERN_VERSION = "non-discriminant-patterns-v1"

# These bounds are deliberately independent: v1 retains its historical
# in-memory parser limit, while v2 streams source objects in bounded batches.
DEFAULT_V1_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
DEFAULT_V2_MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
# Keep the existing name available to callers that used it for the v2 path.
DEFAULT_MAX_DECOMPRESSED_BYTES = DEFAULT_V2_MAX_DECOMPRESSED_BYTES
INGEST_BATCH_SIZE = 8192
MAX_SQLITE_COUNT = (1 << 63) - 1

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
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_FEATURES_SQL = """CREATE TABLE features (
    feature_key BLOB PRIMARY KEY CHECK(length(feature_key) = 32),
    occurrence_count INTEGER NOT NULL CHECK(occurrence_count > 0)
) WITHOUT ROWID"""
_METADATA_SQL = """CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID"""


class GoodwareImportError(ValueError):
    """Operator-facing validation error for an unsupported or corrupt input."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _source_kind(path: Path) -> str | None:
    name = path.name.lower()
    if not name.endswith(".db"):
        return None
    for prefix, kind in _SOURCE_PREFIXES:
        if name.startswith(prefix):
            return kind
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_bounded_json(path: Path, *, max_decompressed_bytes: int) -> dict[str, Any]:
    """Read a v1 source into memory, retaining the established v1 path."""
    if max_decompressed_bytes < 1:
        raise GoodwareImportError("max_decompressed_bytes must be positive")
    with path.open("rb") as raw:
        magic = raw.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    try:
        with opener(path, "rb") as handle:
            payload = handle.read(max_decompressed_bytes + 1)
    except OSError as exc:
        raise GoodwareImportError(f"{path.name}: cannot read database: {exc}") from exc
    if len(payload) > max_decompressed_bytes:
        raise GoodwareImportError(
            f"{path.name}: decompressed JSON exceeds {max_decompressed_bytes} bytes"
        )
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoodwareImportError(f"{path.name}: invalid JSON database") from exc
    if not isinstance(decoded, dict):
        raise GoodwareImportError(f"{path.name}: database root must be a JSON object")
    return decoded


def _unescape_yargen_string(value: str) -> str:
    """Reverse only the two escapes yarGen applies before counting strings."""
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in {"\\", '"'}:
            output.append(value[index + 1])
            index += 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def normalize_value_v1(kind: str, value: str) -> str:
    """Normalize the established v1 JSONL stage representation."""
    if not isinstance(value, str):
        raise GoodwareImportError(f"{kind}: feature key must be a string")
    if kind == "string":
        prefix = "UTF16LE:"
        if value.startswith(prefix):
            normalized = prefix + unicodedata.normalize(
                "NFC", _unescape_yargen_string(value[len(prefix) :])
            )
        else:
            normalized = unicodedata.normalize("NFC", _unescape_yargen_string(value))
        if not normalized:
            raise GoodwareImportError("string: empty feature")
        return normalized
    if kind == "opcode_fragment16":
        normalized = re.sub(r"\s+", "", value).lower()
        if (
            len(normalized) < 16
            or len(normalized) > 32
            or len(normalized) % 2
            or not _HEX_RE.fullmatch(normalized)
        ):
            raise GoodwareImportError(
                "opcode_fragment16: expected 8..16 bytes encoded as hexadecimal"
            )
        return normalized
    if kind == "imphash":
        normalized = value.strip().lower()
        if len(normalized) != 32 or not _HEX_RE.fullmatch(normalized):
            raise GoodwareImportError(
                "imphash: expected lowercase-compatible 32-hex value"
            )
        return normalized
    if kind == "export":
        normalized = unicodedata.normalize("NFC", value)
        if not normalized:
            raise GoodwareImportError("export: empty feature")
        return normalized
    raise GoodwareImportError(f"unsupported feature kind: {kind}")


def normalize_value(kind: str, value: str) -> str:
    """Apply the frozen v2 normalization contract to one source value."""
    if not isinstance(value, str):
        raise GoodwareImportError(f"{kind}: feature key must be a string")
    if kind == "string":
        decoded = _unescape_yargen_string(value)
        marker = "UTF16LE:"
        decoded = decoded.removeprefix(marker)
        normalized = decoded.lower()
        if not normalized:
            raise GoodwareImportError("string: empty feature")
        return normalized
    if kind == "export":
        normalized = value.lower()
        if not normalized:
            raise GoodwareImportError("export: empty feature")
        return normalized
    if kind == "imphash":
        if value == "":
            raise GoodwareImportError(
                "imphash: empty value is only valid as a source sentinel"
            )
        normalized = value.strip().lower()
        if len(normalized) != 32 or not _HEX_RE.fullmatch(normalized):
            raise GoodwareImportError(
                "imphash: expected lowercase-compatible 32-hex value"
            )
        return normalized
    if kind == "opcode_fragment16":
        normalized = re.sub(r"\s+", "", value).lower()
        if (
            len(normalized) < 16
            or len(normalized) > 32
            or len(normalized) % 2
            or not _HEX_RE.fullmatch(normalized)
        ):
            raise GoodwareImportError(
                "opcode_fragment16: expected 8..16 bytes encoded as hexadecimal"
            )
        return normalized
    raise GoodwareImportError(f"unsupported feature kind: {kind}")


def lookup_key(feature_kind: str, normalized_value: str) -> bytes:
    """Return the canonical key-version-v1 digest for an already-normalized feature."""
    if (
        not isinstance(feature_kind, str)
        or feature_kind not in _SUPPORTED_FEATURE_KIND_SET
    ):
        raise GoodwareImportError(f"unsupported feature kind: {feature_kind}")
    if not isinstance(normalized_value, str):
        raise GoodwareImportError("normalized feature value must be a string")
    try:
        kind_bytes = feature_kind.encode("ascii")
    except UnicodeEncodeError as exc:
        raise GoodwareImportError("feature kind must be ASCII") from exc
    return hashlib.sha256(
        KEY_VERSION.encode("ascii")
        + b"\0"
        + kind_bytes
        + b"\0"
        + normalized_value.encode("utf-8")
    ).digest()


canonical_lookup_key = lookup_key


def baseline_fingerprint_sha256(
    source_set_sha256: str,
    *,
    pattern_version: str = NON_DISCRIMINANT_PATTERN_VERSION,
) -> str:
    """Hash the semantic baseline identity, excluding physical artifact data."""
    value = {
        "normalization_version": NORMALIZATION_VERSION,
        "pattern_version": pattern_version,
        "source_set_sha256": source_set_sha256,
    }
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validated_count(kind: str, value: Any, count: Any) -> int:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise GoodwareImportError(
            f"{kind}:{value!r}: occurrence count must be a positive integer"
        )
    if count > MAX_SQLITE_COUNT:
        raise GoodwareImportError(
            f"{kind}:{value!r}: occurrence count exceeds SQLite integer range"
        )
    return count


def _iter_sources(input_dir: Path) -> Iterator[tuple[Path, str]]:
    if not input_dir.is_dir():
        raise GoodwareImportError(f"input directory does not exist: {input_dir}")
    matched: list[tuple[Path, str]] = []
    for path in input_dir.iterdir():
        if not path.is_file():
            continue
        kind = _source_kind(path)
        if kind is not None:
            matched.append((path, kind))
    if not matched:
        raise GoodwareImportError("no supported good-*.db source files found")
    yield from sorted(matched, key=lambda item: item[0].name)


def _iter_v2_sources(input_dir: Path) -> Iterator[tuple[Path, str]]:
    if not input_dir.is_dir():
        raise GoodwareImportError(f"input directory does not exist: {input_dir}")
    paths = sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".db"
        ),
        key=lambda path: path.name,
    )
    if not paths:
        raise GoodwareImportError("no supported good-*.db source files found")
    for path in paths:
        kind = _source_kind(path)
        if kind is None:
            raise GoodwareImportError(f"unsupported source database: {path.name}")
        yield path, kind


def _insert_stage_rows(
    connection: sqlite3.Connection,
    rows: list[tuple[str, str, int]],
) -> None:
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO records(feature_kind, normalized_value, occurrence_count)
        VALUES (?, ?, ?)
        ON CONFLICT(feature_kind, normalized_value) DO UPDATE SET
            occurrence_count = records.occurrence_count + excluded.occurrence_count
        """,
        rows,
    )


def _insert_feature_rows(
    connection: sqlite3.Connection,
    rows: list[tuple[bytes, int]],
) -> None:
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO features(feature_key, occurrence_count)
        VALUES (?, ?)
        ON CONFLICT(feature_key) DO UPDATE SET
            occurrence_count = features.occurrence_count + excluded.occurrence_count
        """,
        rows,
    )


def _aggregate(
    input_dir: Path,
    sqlite_path: Path,
    *,
    max_decompressed_bytes: int,
) -> list[dict[str, Any]]:
    """Aggregate the old v1 representation with bounded write batches."""
    connection = sqlite3.connect(sqlite_path)
    connection.execute(
        """
        CREATE TABLE records (
            feature_kind TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
            PRIMARY KEY (feature_kind, normalized_value)
        ) WITHOUT ROWID
        """
    )
    sources: list[dict[str, Any]] = []
    try:
        for path, kind in _iter_sources(input_dir):
            raw = _read_bounded_json(
                path, max_decompressed_bytes=max_decompressed_bytes
            )
            source_occurrences = 0
            rows: list[tuple[str, str, int]] = []
            for original_value, original_count in raw.items():
                count = _validated_count(kind, original_value, original_count)
                source_occurrences += count

                # This is retained solely for the established v1 stage path.
                if kind in {"imphash", "string"} and original_value == "":
                    continue

                normalized = normalize_value_v1(kind, original_value)
                rows.append((kind, normalized, count))
                if len(rows) >= INGEST_BATCH_SIZE:
                    with connection:
                        _insert_stage_rows(connection, rows)
                    rows.clear()
            with connection:
                _insert_stage_rows(connection, rows)
            sources.append(
                {
                    "filename": path.name,
                    "feature_kind": kind,
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                    "entry_count": len(raw),
                    "occurrence_sum": source_occurrences,
                }
            )
    finally:
        connection.close()
    return sources


def _source_set_sha256(sources: list[dict[str, Any]]) -> str:
    stable = [
        {
            "filename": item["filename"],
            "feature_kind": item["feature_kind"],
            "sha256": item["sha256"],
            "size": item["size"],
        }
        for item in sources
    ]
    return hashlib.sha256(_canonical_json(stable).encode("utf-8")).hexdigest()


def _write_records(sqlite_path: Path, destination: Path) -> tuple[str, int, int]:
    connection = sqlite3.connect(sqlite_path)
    digest = hashlib.sha256()
    record_count = 0
    occurrence_sum = 0
    try:
        with destination.open("wb") as output:
            cursor = connection.execute(
                """
                SELECT feature_kind, normalized_value, occurrence_count
                FROM records
                ORDER BY feature_kind, normalized_value
                """
            )
            for kind, value, count in cursor:
                record = {
                    "feature_kind": kind,
                    "normalized_value": value,
                    "occurrence_count": int(count),
                }
                line = (_canonical_json(record) + "\n").encode("utf-8")
                output.write(line)
                digest.update(line)
                record_count += 1
                occurrence_sum += int(count)
    finally:
        connection.close()
    return digest.hexdigest(), record_count, occurrence_sum


def build_stage(
    input_dir: Path,
    output_dir: Path,
    *,
    max_decompressed_bytes: int = DEFAULT_V1_MAX_DECOMPRESSED_BYTES,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build the already-supported v1 JSONL stage."""
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise GoodwareImportError(f"output directory is not empty: {output_dir}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".goodware-stage-", dir=output_dir.parent
    ) as temp_name:
        temp_dir = Path(temp_name)
        sqlite_path = temp_dir / "aggregate.sqlite3"
        sources = _aggregate(
            input_dir,
            sqlite_path,
            max_decompressed_bytes=max_decompressed_bytes,
        )
        records_sha256, record_count, occurrence_sum = _write_records(
            sqlite_path, temp_dir / RECORDS_FILENAME
        )
        manifest = {
            "schema_version": V1_SCHEMA_VERSION,
            "source_format": SOURCE_FORMAT,
            "source_set_sha256": _source_set_sha256(sources),
            "records_sha256": records_sha256,
            "record_count": record_count,
            "occurrence_sum": occurrence_sum,
            "sources": sources,
        }
        (temp_dir / MANIFEST_FILENAME).write_text(
            _canonical_json(manifest) + "\n",
            encoding="utf-8",
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        for filename in (RECORDS_FILENAME, MANIFEST_FILENAME):
            target = output_dir / filename
            if target.exists():
                if not overwrite:
                    raise GoodwareImportError(
                        f"refusing to replace existing file: {target}"
                    )
                target.unlink()
            shutil.move(str(temp_dir / filename), target)
    return manifest


class _StreamingJsonObject:
    """Incrementally parse the object-shaped yarGen counter JSON format."""

    _CHUNK_SIZE = 64 * 1024

    def __init__(self, raw: BinaryIO, *, max_decompressed_bytes: int) -> None:
        self._raw = raw
        self._max_decompressed_bytes = max_decompressed_bytes
        self._bytes_read = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._position = 0
        self._eof = False
        self._json_decoder = json.JSONDecoder()

    def _fill(self) -> None:
        if self._eof:
            return
        chunk = self._raw.read(self._CHUNK_SIZE)
        if not chunk:
            self._buffer += self._decoder.decode(b"", final=True)
            self._eof = True
            return
        self._bytes_read += len(chunk)
        if self._bytes_read > self._max_decompressed_bytes:
            raise GoodwareImportError(
                f"source JSON exceeds {self._max_decompressed_bytes} decompressed bytes"
            )
        self._buffer += self._decoder.decode(chunk, final=False)

    def _compact(self) -> None:
        if self._position >= self._CHUNK_SIZE:
            self._buffer = self._buffer[self._position :]
            self._position = 0

    def _skip_whitespace(self) -> None:
        while True:
            while (
                self._position < len(self._buffer)
                and self._buffer[self._position].isspace()
            ):
                self._position += 1
            self._compact()
            if self._position < len(self._buffer) or self._eof:
                return
            self._fill()

    def _peek(self) -> str | None:
        while self._position >= len(self._buffer) and not self._eof:
            self._fill()
        if self._position >= len(self._buffer):
            return None
        return self._buffer[self._position]

    def _parse_value(self) -> Any:
        while True:
            try:
                value, end = self._json_decoder.raw_decode(self._buffer, self._position)
            except json.JSONDecodeError:
                if self._eof:
                    raise
                self._fill()
                continue

            # A valid JSON scalar can be only a prefix of the real token when
            # the token is split exactly at the current chunk boundary.
            # Example: the source contains 1000 but the buffer currently ends
            # after 10. raw_decode() accepts 10, so read ahead before accepting
            # any value that reaches the current non-EOF buffer boundary.
            if end == len(self._buffer) and not self._eof:
                self._fill()
                continue

            self._position = end
            self._compact()
            return value

    def _consume(self, expected: str) -> None:
        actual = self._peek()
        if actual != expected:
            raise GoodwareImportError(
                f"invalid source JSON: expected {expected!r}, got {actual!r}"
            )
        self._position += 1

    def _finish(self) -> None:
        self._skip_whitespace()
        if self._peek() is not None:
            raise GoodwareImportError("invalid source JSON: trailing data")

    def items(self) -> Iterator[tuple[str, Any]]:
        if self._max_decompressed_bytes < 1:
            raise GoodwareImportError("max_decompressed_bytes must be positive")
        self._skip_whitespace()
        self._consume("{")
        self._skip_whitespace()
        if self._peek() == "}":
            self._position += 1
            self._finish()
            return
        while True:
            self._skip_whitespace()
            key = self._parse_value()
            if not isinstance(key, str):
                raise GoodwareImportError(
                    "invalid source JSON: object key is not a string"
                )
            self._skip_whitespace()
            self._consume(":")
            self._skip_whitespace()
            value = self._parse_value()
            yield key, value
            self._skip_whitespace()
            marker = self._peek()
            if marker == ",":
                self._position += 1
                continue
            if marker == "}":
                self._position += 1
                self._finish()
                return
            raise GoodwareImportError(
                f"invalid source JSON: expected ',' or '}}', got {marker!r}"
            )


def _iter_source_items(
    path: Path,
    *,
    max_decompressed_bytes: int,
) -> Iterator[tuple[str, Any]]:
    if max_decompressed_bytes < 1:
        raise GoodwareImportError("max_decompressed_bytes must be positive")
    with path.open("rb") as raw:
        magic = raw.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    try:
        with opener(path, "rb") as raw:
            yield from _StreamingJsonObject(
                raw,
                max_decompressed_bytes=max_decompressed_bytes,
            ).items()
    except GoodwareImportError as exc:
        raise GoodwareImportError(f"{path.name}: {exc}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoodwareImportError(f"{path.name}: invalid JSON database") from exc


def _apply_build_pragmas(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")


def _aggregate_v2(
    input_dir: Path,
    index_path: Path,
    *,
    max_decompressed_bytes: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Stream v2 sources directly into the final hashed-key table."""
    connection = sqlite3.connect(index_path)
    _apply_build_pragmas(connection)
    connection.execute(_FEATURES_SQL)
    sources: list[dict[str, Any]] = []
    occurrence_sum = 0
    try:
        for path, kind in _iter_v2_sources(input_dir):
            source_occurrences = 0
            entry_count = 0
            rows: list[tuple[bytes, int]] = []
            for original_value, original_count in _iter_source_items(
                path,
                max_decompressed_bytes=max_decompressed_bytes,
            ):
                count = _validated_count(kind, original_value, original_count)
                source_occurrences += count
                entry_count += 1

                # Raw yarGen empty string/imphash entries are source sentinels,
                # not usable features. Keep them in per-source metadata only.
                if kind in {"string", "imphash"} and original_value == "":
                    continue

                normalized = normalize_value(kind, original_value)
                rows.append((lookup_key(kind, normalized), count))
                occurrence_sum += count
                if len(rows) >= INGEST_BATCH_SIZE:
                    with connection:
                        _insert_feature_rows(connection, rows)
                    rows.clear()
            with connection:
                _insert_feature_rows(connection, rows)
            sources.append(
                {
                    "filename": path.name,
                    "feature_kind": kind,
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                    "entry_count": entry_count,
                    "occurrence_sum": source_occurrences,
                }
            )
        row = connection.execute("SELECT COUNT(*) FROM features").fetchone()
        if row is None:
            raise GoodwareImportError("cannot count v2 features")
        record_count = int(row[0])
    except sqlite3.Error as exc:
        raise GoodwareImportError(f"cannot write v2 features: {exc}") from exc
    finally:
        connection.close()
    return sources, record_count, occurrence_sum


def _metadata_values(
    *,
    source_set_sha256: str,
    record_count: int,
    occurrence_sum: int,
    baseline_fingerprint: str,
) -> dict[str, str]:
    return {
        "baseline_fingerprint_sha256": baseline_fingerprint,
        "index_format_version": INDEX_FORMAT_VERSION,
        "key_version": KEY_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "occurrence_sum": str(occurrence_sum),
        "pattern_version": NON_DISCRIMINANT_PATTERN_VERSION,
        "record_count": str(record_count),
        "schema_version": SCHEMA_VERSION,
        "source_format": SOURCE_FORMAT,
        "source_set_sha256": source_set_sha256,
    }


def _write_v2_index(
    index_path: Path,
    *,
    metadata: dict[str, str],
) -> None:
    index = sqlite3.connect(index_path)
    _apply_build_pragmas(index)
    try:
        index.execute(_METADATA_SQL)
        try:
            with index:
                index.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    sorted(metadata.items()),
                )
        except sqlite3.Error as exc:
            raise GoodwareImportError("cannot write v2 SQLite artifact") from exc
    finally:
        index.close()


def _read_manifest(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / MANIFEST_FILENAME
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoodwareImportError("invalid or missing manifest.json") from exc
    if not isinstance(manifest, dict):
        raise GoodwareImportError("manifest.json root must be an object")
    if raw != (_canonical_json(manifest) + "\n").encode("utf-8"):
        raise GoodwareImportError("manifest.json is not canonical JSON")
    return manifest


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise GoodwareImportError(f"manifest {field} must be a lowercase SHA-256")
    return value


def _validate_source_filename(filename: Any) -> str:
    if not isinstance(filename, str):
        raise GoodwareImportError("manifest source filename must be a string")
    path = Path(filename)
    if path.is_absolute() or path.name != filename or ".." in path.parts:
        raise GoodwareImportError(f"unsafe source filename: {filename!r}")
    return filename


def _validate_v2_manifest(manifest: dict[str, Any]) -> None:
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
        value = manifest[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GoodwareImportError(
                f"manifest {field} must be a non-negative integer"
            )
    if manifest["index_size"] == 0:
        raise GoodwareImportError("manifest index_size must be positive")

    sources = manifest["sources"]
    if not isinstance(sources, list) or not sources:
        raise GoodwareImportError("manifest sources must be a non-empty list")
    source_keys = {
        "filename",
        "feature_kind",
        "sha256",
        "size",
        "entry_count",
        "occurrence_sum",
    }
    filenames: list[str] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != source_keys:
            raise GoodwareImportError("invalid source manifest entry")
        filename = _validate_source_filename(source["filename"])
        kind = source["feature_kind"]
        if (
            not isinstance(kind, str)
            or kind not in _SUPPORTED_FEATURE_KIND_SET
            or _source_kind(Path(filename)) != kind
        ):
            raise GoodwareImportError("invalid source feature kind")
        _require_sha256(source["sha256"], f"source {filename} sha256")
        for field in ("size", "entry_count", "occurrence_sum"):
            value = source[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GoodwareImportError(f"invalid source {filename} {field}")
        filenames.append(filename)
    if filenames != sorted(filenames) or len(set(filenames)) != len(filenames):
        raise GoodwareImportError("manifest sources are not sorted and unique")
    if _source_set_sha256(sources) != source_set:
        raise GoodwareImportError("source_set_sha256 does not match sources")


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


def _verify_sqlite_index(
    index_path: Path, manifest: dict[str, Any], *, deep: bool
) -> None:
    for suffix in ("-wal", "-shm"):
        if index_path.with_name(index_path.name + suffix).exists():
            raise GoodwareImportError(
                f"SQLite index has an unexpected {suffix} sidecar"
            )
    try:
        connection = sqlite3.connect(
            f"{index_path.resolve().as_uri()}?mode=ro", uri=True
        )
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

        expected_metadata = _metadata_values(
            source_set_sha256=manifest["source_set_sha256"],
            record_count=manifest["record_count"],
            occurrence_sum=manifest["occurrence_sum"],
            baseline_fingerprint=manifest["baseline_fingerprint_sha256"],
        )
        metadata_rows = list(connection.execute("SELECT key, value FROM metadata"))
        if len(metadata_rows) != len(expected_metadata) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata_rows
        ):
            raise GoodwareImportError("SQLite metadata is malformed")
        if dict(metadata_rows) != expected_metadata:
            raise GoodwareImportError("SQLite metadata does not match manifest")

        if deep:
            bad_row = connection.execute(
                """
                SELECT 1 FROM features
                WHERE typeof(feature_key) != 'blob'
                   OR length(feature_key) != 32
                   OR typeof(occurrence_count) != 'integer'
                   OR occurrence_count <= 0
                LIMIT 1
                """
            ).fetchone()
            if bad_row is not None:
                raise GoodwareImportError(
                    "SQLite index contains malformed feature rows"
                )
            try:
                stats = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(occurrence_count), 0) FROM features"
                ).fetchone()
            except sqlite3.Error as exc:
                raise GoodwareImportError(
                    "cannot inspect SQLite feature counts"
                ) from exc
            if (
                stats is None
                or int(stats[0]) != manifest["record_count"]
                or int(stats[1]) != manifest["occurrence_sum"]
            ):
                raise GoodwareImportError("SQLite feature counts do not match manifest")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise GoodwareImportError("SQLite integrity_check failed")
    except GoodwareImportError:
        raise
    except (IndexError, TypeError, ValueError, sqlite3.Error) as exc:
        raise GoodwareImportError("invalid SQLite index") from exc
    finally:
        connection.close()


def _verify_sources(source_dir: Path, manifest: dict[str, Any]) -> None:
    actual_sources: list[dict[str, Any]] = []
    for path, kind in _iter_v2_sources(source_dir):
        actual_sources.append(
            {
                "filename": path.name,
                "feature_kind": kind,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    expected_sources = [
        {
            "filename": source["filename"],
            "feature_kind": source["feature_kind"],
            "sha256": source["sha256"],
            "size": source["size"],
        }
        for source in manifest["sources"]
    ]
    if actual_sources != expected_sources:
        raise GoodwareImportError("source directory does not match source_set_sha256")
    if _source_set_sha256(actual_sources) != manifest["source_set_sha256"]:
        raise GoodwareImportError("source directory source-set hash mismatch")


def verify_index(
    output_dir: Path,
    source_dir: Path | None = None,
    *,
    deep: bool = False,
) -> dict[str, Any]:
    """Cheap structural/integrity verification of a v2 artifact.

    This intentionally does not parse source JSON or make a Python pass over
    all features.  ``deep=True`` additionally runs SQLite's full integrity
    check and is exposed as ``deep_verify_index`` for explicit operator use.
    """
    output_dir = output_dir.resolve()
    if not output_dir.is_dir():
        raise GoodwareImportError(
            f"index output directory does not exist: {output_dir}"
        )
    manifest = _read_manifest(output_dir)
    _validate_v2_manifest(manifest)
    index_path = output_dir / INDEX_FILENAME
    if not index_path.is_file():
        raise GoodwareImportError(f"missing {INDEX_FILENAME}")
    if index_path.stat().st_size != manifest["index_size"]:
        raise GoodwareImportError("SQLite index size does not match manifest")
    if _sha256_file(index_path) != manifest["index_sha256"]:
        raise GoodwareImportError("SQLite index SHA-256 does not match manifest")
    if source_dir is not None:
        _verify_sources(source_dir.resolve(), manifest)
    _verify_sqlite_index(index_path, manifest, deep=deep)
    return manifest


def deep_verify_index(
    output_dir: Path, source_dir: Path | None = None
) -> dict[str, Any]:
    return verify_index(output_dir, source_dir, deep=True)


def lookup_feature(
    index_path: Path, feature_kind: str, normalized_value: str
) -> int | None:
    """Look up an already-normalized feature; absence returns ``None``."""
    index_path = index_path.resolve()
    if index_path.is_dir():
        index_path /= INDEX_FILENAME
    key = lookup_key(feature_kind, normalized_value)
    try:
        connection = sqlite3.connect(f"{index_path.as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT occurrence_count FROM features WHERE feature_key = ?",
                (key,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise GoodwareImportError("cannot query read-only SQLite index") from exc
    return None if row is None else int(row[0])


lookup_count = lookup_feature


def _promote_artifact(artifact_dir: Path, output_dir: Path, *, overwrite: bool) -> None:
    if not output_dir.exists():
        os.replace(artifact_dir, output_dir)
        return
    if not output_dir.is_dir():
        raise GoodwareImportError(f"output path is not a directory: {output_dir}")
    if not overwrite and any(output_dir.iterdir()):
        raise GoodwareImportError(f"output directory is not empty: {output_dir}")
    if not any(output_dir.iterdir()):
        output_dir.rmdir()
        os.replace(artifact_dir, output_dir)
        return
    for filename in (INDEX_FILENAME, MANIFEST_FILENAME):
        os.replace(artifact_dir / filename, output_dir / filename)


def build_index(
    input_dir: Path,
    output_dir: Path,
    *,
    max_decompressed_bytes: int = DEFAULT_V2_MAX_DECOMPRESSED_BYTES,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build the immutable v2 SQLite artifact directly from yarGen sources."""
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise GoodwareImportError(f"output path is not a directory: {output_dir}")
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise GoodwareImportError(f"output directory is not empty: {output_dir}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".goodware-index-", dir=output_dir.parent
    ) as temp_name:
        work_dir = Path(temp_name)
        artifact_dir = work_dir / "artifact"
        artifact_dir.mkdir()
        index_path = artifact_dir / INDEX_FILENAME
        sources, record_count, occurrence_sum = _aggregate_v2(
            input_dir,
            index_path,
            max_decompressed_bytes=max_decompressed_bytes,
        )
        source_set = _source_set_sha256(sources)
        # The fingerprint deliberately excludes index_sha256 and index_size.
        baseline = baseline_fingerprint_sha256(source_set)
        _write_v2_index(
            index_path,
            metadata=_metadata_values(
                source_set_sha256=source_set,
                record_count=record_count,
                occurrence_sum=occurrence_sum,
                baseline_fingerprint=baseline,
            ),
        )
        index_sha256 = _sha256_file(index_path)
        index_size = index_path.stat().st_size
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "source_format": SOURCE_FORMAT,
            "source_set_sha256": source_set,
            "normalization_version": NORMALIZATION_VERSION,
            "key_version": KEY_VERSION,
            "index_format_version": INDEX_FORMAT_VERSION,
            "baseline_fingerprint_sha256": baseline,
            "record_count": record_count,
            "occurrence_sum": occurrence_sum,
            "index_sha256": index_sha256,
            "index_size": index_size,
            "sources": sources,
        }
        manifest_path = artifact_dir / MANIFEST_FILENAME
        manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        index_path.chmod(index_path.stat().st_mode & ~0o222)
        manifest_path.chmod(manifest_path.stat().st_mode & ~0o222)
        verify_index(artifact_dir)
        _promote_artifact(artifact_dir, output_dir, overwrite=overwrite)
    return manifest


# Descriptive aliases for callers that want to distinguish v2 from the
# retained v1 stage API without depending on the CLI spelling.
build_v2 = build_index
verify_v2 = verify_index


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="normalize goodware DB files into the legacy v1 stage"
    )
    build.add_argument("input_dir", type=Path)
    build.add_argument("output_dir", type=Path)
    build.add_argument(
        "--max-decompressed-bytes",
        type=int,
        default=DEFAULT_V1_MAX_DECOMPRESSED_BYTES,
    )
    build.add_argument("--overwrite", action="store_true")

    verify = subparsers.add_parser("verify", help="verify an existing legacy v1 stage")
    verify.add_argument("output_dir", type=Path)

    build_index_parser = subparsers.add_parser(
        "build-index", help="build the v2 read-only SQLite goodware index"
    )
    build_index_parser.add_argument("input_dir", type=Path)
    build_index_parser.add_argument("output_dir", type=Path)
    build_index_parser.add_argument(
        "--max-decompressed-bytes",
        type=int,
        default=DEFAULT_V2_MAX_DECOMPRESSED_BYTES,
    )
    build_index_parser.add_argument("--overwrite", action="store_true")

    for command, help_text in (
        ("verify-index", "cheap structural/integrity verification of a v2 index"),
        ("deep-verify-index", "full SQLite integrity verification of a v2 index"),
    ):
        verifier = subparsers.add_parser(command, help=help_text)
        verifier.add_argument("output_dir", type=Path)
        verifier.add_argument("--source-dir", type=Path)
    return parser


def verify_stage(output_dir: Path) -> dict[str, Any]:
    """Verify the established v1 JSONL stage."""
    manifest_path = output_dir / MANIFEST_FILENAME
    records_path = output_dir / RECORDS_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoodwareImportError("invalid or missing manifest.json") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != V1_SCHEMA_VERSION
    ):
        raise GoodwareImportError("unsupported staging schema")
    if not records_path.is_file():
        raise GoodwareImportError("missing records.jsonl")

    digest = hashlib.sha256()
    record_count = 0
    occurrence_sum = 0
    previous: tuple[str, str] | None = None
    with records_path.open("rb") as handle:
        for raw_line in handle:
            digest.update(raw_line)
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise GoodwareImportError(
                    f"invalid records.jsonl line {record_count + 1}"
                ) from exc
            if not isinstance(record, dict):
                raise GoodwareImportError("records.jsonl entries must be JSON objects")
            expected_keys = {"feature_kind", "normalized_value", "occurrence_count"}
            if set(record) != expected_keys:
                raise GoodwareImportError("records.jsonl entry has unexpected fields")
            kind = record["feature_kind"]
            value = record["normalized_value"]
            count = record["occurrence_count"]
            if not isinstance(kind, str) or not isinstance(value, str):
                raise GoodwareImportError(
                    "records.jsonl feature fields must be strings"
                )
            # v1 strings are checked as target text because yarGen unescaping is
            # intentionally lossy and must not be applied a second time.
            if kind == "string":
                if not value or unicodedata.normalize("NFC", value) != value:
                    raise GoodwareImportError(
                        "records.jsonl contains a non-normalized feature"
                    )
            elif normalize_value_v1(kind, value) != value:
                raise GoodwareImportError(
                    "records.jsonl contains a non-normalized feature"
                )
            _validated_count(kind, value, count)
            canonical = (_canonical_json(record) + "\n").encode("utf-8")
            if raw_line != canonical:
                raise GoodwareImportError("records.jsonl is not canonical JSONL")
            key = (kind, value)
            if previous is not None and key <= previous:
                raise GoodwareImportError(
                    "records.jsonl is not strictly sorted or contains duplicates"
                )
            previous = key
            record_count += 1
            occurrence_sum += count

    if digest.hexdigest() != manifest.get("records_sha256"):
        raise GoodwareImportError("records.jsonl SHA-256 does not match manifest")
    if record_count != manifest.get("record_count"):
        raise GoodwareImportError("record_count does not match manifest")
    if occurrence_sum != manifest.get("occurrence_sum"):
        raise GoodwareImportError("occurrence_sum does not match manifest")
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            manifest = build_stage(
                args.input_dir,
                args.output_dir,
                max_decompressed_bytes=args.max_decompressed_bytes,
                overwrite=args.overwrite,
            )
        elif args.command == "verify":
            manifest = verify_stage(args.output_dir)
        elif args.command == "build-index":
            manifest = build_index(
                args.input_dir,
                args.output_dir,
                max_decompressed_bytes=args.max_decompressed_bytes,
                overwrite=args.overwrite,
            )
        else:
            manifest = verify_index(
                args.output_dir,
                args.source_dir,
                deep=args.command == "deep-verify-index",
            )
    except GoodwareImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(_canonical_json(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

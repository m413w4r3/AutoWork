#!/usr/bin/env python3
"""Normalize local yarGen goodware databases into deterministic AutoWork staging files.

This tool is deliberately offline and database-independent. It accepts the gzip-JSON
Counter files used by yarGen 0.23+ (plain JSON is accepted for operator-generated
variants), aggregates split parts with bounded temporary SQLite storage, and writes:

- records.jsonl: canonical, sorted feature/count records
- manifest.json: source hashes and deterministic aggregate hashes

It performs no network access and does not depend on AutoWork application code.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "autowork-goodware-stage-v1"
SOURCE_FORMAT = "yargen-gzip-json-counter-v1"
RECORDS_FILENAME = "records.jsonl"
MANIFEST_FILENAME = "manifest.json"
DEFAULT_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_SQLITE_COUNT = (1 << 63) - 1

_SOURCE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("good-strings", "string"),
    ("good-opcodes", "opcode_fragment16"),
    ("good-imphashes", "imphash"),
    ("good-imphash", "imphash"),
    ("good-exports", "export"),
)
_HEX_RE = re.compile(r"^[0-9a-f]+$")


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
        if char == "\\" and index + 1 < len(value) and value[index + 1] in {'\\', '"'}:
            output.append(value[index + 1])
            index += 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def normalize_value(kind: str, value: str) -> str:
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
            raise GoodwareImportError("imphash: expected lowercase-compatible 32-hex value")
        return normalized
    if kind == "export":
        normalized = unicodedata.normalize("NFC", value)
        if not normalized:
            raise GoodwareImportError("export: empty feature")
        return normalized
    raise GoodwareImportError(f"unsupported feature kind: {kind}")


def _validated_count(kind: str, value: str, count: Any) -> int:
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


def _aggregate(
    input_dir: Path,
    sqlite_path: Path,
    *,
    max_decompressed_bytes: int,
) -> list[dict[str, Any]]:
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
            raw = _read_bounded_json(path, max_decompressed_bytes=max_decompressed_bytes)
            source_occurrences = 0
            rows: list[tuple[str, str, int]] = []
            for original_value, original_count in raw.items():
                count = _validated_count(kind, original_value, original_count)
                source_occurrences += count

                # Official yarGen goodware shards may use the empty string as
                # a sentinel for unavailable imphashes or empty string entries.
                # Preserve source statistics, but never persist it as a feature.
                if kind in {"imphash", "string"} and original_value == "":
                    continue

                normalized = normalize_value(kind, original_value)
                rows.append((kind, normalized, count))
            with connection:
                connection.executemany(
                    """
                    INSERT INTO records(feature_kind, normalized_value, occurrence_count)
                    VALUES (?, ?, ?)
                    ON CONFLICT(feature_kind, normalized_value) DO UPDATE SET
                        occurrence_count = records.occurrence_count + excluded.occurrence_count
                    """,
                    rows,
                )
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
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    overwrite: bool = False,
) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise GoodwareImportError(f"output directory is not empty: {output_dir}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".goodware-stage-", dir=output_dir.parent) as temp_name:
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
            "schema_version": SCHEMA_VERSION,
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
                    raise GoodwareImportError(f"refusing to replace existing file: {target}")
                target.unlink()
            shutil.move(str(temp_dir / filename), target)
    return manifest


def verify_stage(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / MANIFEST_FILENAME
    records_path = output_dir / RECORDS_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoodwareImportError("invalid or missing manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
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
                raise GoodwareImportError("records.jsonl feature fields must be strings")
            # Source normalization for yarGen strings is intentionally lossy:
            # doubled backslashes and escaped quotes are unescaped during build.
            # Re-applying that source transformation here would therefore not
            # be idempotent for some legitimate already-normalized strings.
            #
            # Stage validation checks the canonical target representation
            # instead: strings must be non-empty NFC text. Other feature kinds
            # have idempotent normalization and can still use normalize_value().
            if kind == "string":
                if not value or unicodedata.normalize("NFC", value) != value:
                    raise GoodwareImportError(
                        "records.jsonl contains a non-normalized feature"
                    )
            elif normalize_value(kind, value) != value:
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="normalize goodware DB files into staging output"
    )
    build.add_argument("input_dir", type=Path)
    build.add_argument("output_dir", type=Path)
    build.add_argument(
        "--max-decompressed-bytes",
        type=int,
        default=DEFAULT_MAX_DECOMPRESSED_BYTES,
        help="maximum decompressed JSON size accepted per source file",
    )
    build.add_argument("--overwrite", action="store_true")

    verify = subparsers.add_parser("verify", help="verify an existing staging directory")
    verify.add_argument("output_dir", type=Path)
    return parser


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
        else:
            manifest = verify_stage(args.output_dir)
    except GoodwareImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(_canonical_json(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

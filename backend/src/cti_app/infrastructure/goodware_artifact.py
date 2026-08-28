from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.goodware_index import (
    INDEX_FILENAME,
    INDEX_FORMAT_VERSION,
    KEY_VERSION,
    MANIFEST_FILENAME,
    NORMALIZATION_VERSION,
    SCHEMA_VERSION,
    SOURCE_FORMAT,
    SUPPORTED_FEATURE_KINDS,
    GoodwareImportError,
    baseline_fingerprint_sha256,
    canonical_json,
    source_set_sha256,
)
from cti_app.infrastructure.goodware_index import sha256_file, verify_sqlite_index

_SOURCE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("good-strings", "string"),
    ("good-opcodes", "opcode_fragment16"),
    ("good-imphashes", "imphash"),
    ("good-imphash", "imphash"),
    ("good-exports", "export"),
)
_SUPPORTED_FEATURE_KIND_SET = frozenset(SUPPORTED_FEATURE_KINDS)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 1024 * 1024


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


def _non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GoodwareImportError(f"manifest {field} must be a non-negative integer")
    return value


def validate_manifest(manifest: object) -> dict[str, Any]:
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
    if source_set_sha256(sources) != source_set:
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
        canonical = (canonical_json(manifest) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GoodwareImportError("manifest.json is not canonical JSON") from exc
    if raw != canonical:
        raise GoodwareImportError("manifest.json is not canonical JSON")
    return validate_manifest(manifest)


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
                "sha256": sha256_file(path),
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
    if source_set_sha256(actual) != manifest["source_set_sha256"]:
        raise GoodwareImportError("source directory source-set hash mismatch")


def _verify_index_file(index_path: Path, manifest: Mapping[str, object]) -> None:
    try:
        size = index_path.stat().st_size
    except OSError as exc:
        raise GoodwareImportError(f"missing {INDEX_FILENAME}") from exc
    if size != manifest["index_size"]:
        raise GoodwareImportError("SQLite index size does not match manifest")
    if sha256_file(index_path) != manifest["index_sha256"]:
        raise GoodwareImportError("SQLite index SHA-256 does not match manifest")


def validate_artifact(artifact_dir: Path, source_dir: Path) -> dict[str, Any]:
    if not artifact_dir.is_dir():
        raise GoodwareImportError(f"artifact directory does not exist: {artifact_dir}")
    manifest = _read_manifest(artifact_dir / MANIFEST_FILENAME)
    index_path = artifact_dir / INDEX_FILENAME
    if not index_path.is_file():
        raise GoodwareImportError(f"missing {INDEX_FILENAME}")
    _verify_index_file(index_path, manifest)
    _verify_sources(source_dir, manifest)
    verify_sqlite_index(index_path, manifest)
    return manifest


def ensure_ingested_descriptor(
    descriptor: BlobDescriptor,
    *,
    expected_sha256: str,
    expected_size: int,
    label: str,
) -> None:
    if descriptor.sha256 != expected_sha256 or descriptor.size != expected_size:
        raise GoodwareImportError(f"ingested {label} does not match validated artifact")


def canonical_manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    return (canonical_json(manifest) + "\n").encode("utf-8")


def manifest_sha256(manifest: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


# Compatibility for callers that used the original application-local helpers.
_canonical_json = canonical_json
_source_set_sha256 = source_set_sha256
_validate_manifest = validate_manifest
_validate_artifact = validate_artifact
_ensure_ingested_descriptor = ensure_ingested_descriptor

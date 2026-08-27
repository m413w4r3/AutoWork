# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from cti_app.domain.goodware import GoodwareFeature

SCHEMA_VERSION = "autowork-goodware-stage-v1"
MANIFEST_FILENAME = "manifest.json"
RECORDS_FILENAME = "records.jsonl"


class GoodwareStageError(ValueError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_source(source_dir: Path, filename: str) -> Path:
    path = Path(filename)
    if path.is_absolute() or path.name != filename or ".." in path.parts:
        raise GoodwareStageError(f"unsafe source filename: {filename!r}")
    return source_dir / path


@dataclass(frozen=True, slots=True, kw_only=True)
class GoodwareStage:
    stage_dir: Path
    source_dir: Path
    manifest: dict[str, object]

    def iter_features(self) -> Iterator[GoodwareFeature]:
        records_path = self.stage_dir / RECORDS_FILENAME
        previous: tuple[str, str] | None = None
        with records_path.open("rb") as handle:
            for number, raw_line in enumerate(handle, 1):
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise GoodwareStageError(f"invalid records.jsonl line {number}") from exc
                if not isinstance(record, dict) or set(record) != {"feature_kind", "normalized_value", "occurrence_count"}:
                    raise GoodwareStageError("records.jsonl entry has unexpected fields")
                kind, value, count = record["feature_kind"], record["normalized_value"], record["occurrence_count"]
                if not isinstance(kind, str) or not isinstance(value, str) or isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise GoodwareStageError("invalid records.jsonl feature")
                canonical = (_canonical(record) + "\n").encode()
                if raw_line != canonical:
                    raise GoodwareStageError("records.jsonl is not canonical JSONL")
                key = (kind, value)
                if previous is not None and key <= previous:
                    raise GoodwareStageError("records.jsonl is not strictly sorted")
                previous = key
                yield GoodwareFeature(feature_kind=kind, normalized_value=value, occurrence_count=count)


def load_stage(stage_dir: Path, source_dir: Path) -> GoodwareStage:
    stage_dir, source_dir = stage_dir.resolve(), source_dir.resolve()
    try:
        manifest = json.loads((stage_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoodwareStageError("invalid or missing manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise GoodwareStageError("unsupported staging schema")
    records_path = stage_dir / RECORDS_FILENAME
    if not records_path.is_file():
        raise GoodwareStageError("missing records.jsonl")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise GoodwareStageError("manifest sources must be non-empty")
    stable_sources: list[dict[str, object]] = []
    listed_filenames: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not all(key in source for key in ("filename", "feature_kind", "sha256", "size")):
            raise GoodwareStageError("invalid source manifest entry")
        filename, expected_sha, expected_size = source["filename"], source["sha256"], source["size"]
        if not isinstance(filename, str) or not isinstance(expected_sha, str) or not isinstance(expected_size, int) or expected_size < 0:
            raise GoodwareStageError("invalid source manifest values")
        if filename in listed_filenames:
            raise GoodwareStageError("duplicate source manifest filename")
        listed_filenames.add(filename)
        path = _safe_source(source_dir, filename)
        if not path.is_file() or path.stat().st_size != expected_size or _sha256(path) != expected_sha:
            raise GoodwareStageError(f"source does not match manifest: {filename}")
        stable_sources.append({"filename": filename, "feature_kind": source["feature_kind"], "sha256": expected_sha, "size": expected_size})
    actual_filenames = {path.name for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() == ".db"}
    if actual_filenames != listed_filenames:
        raise GoodwareStageError("manifest does not cover every source .db")
    source_set = hashlib.sha256(_canonical(stable_sources).encode()).hexdigest()
    if source_set != manifest.get("source_set_sha256"):
        raise GoodwareStageError("source_set_sha256 does not match manifest")
    digest = hashlib.sha256()
    record_count = occurrence_sum = 0
    with records_path.open("rb") as handle:
        for raw_line in handle:
            digest.update(raw_line)
            record_count += 1
            try:
                value = json.loads(raw_line)["occurrence_count"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise GoodwareStageError("invalid records.jsonl") from exc
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise GoodwareStageError("invalid occurrence_count")
            occurrence_sum += value
    if digest.hexdigest() != manifest.get("records_sha256"):
        raise GoodwareStageError("records.jsonl SHA-256 does not match manifest")
    if record_count != manifest.get("record_count") or occurrence_sum != manifest.get("occurrence_sum"):
        raise GoodwareStageError("record_count/occurrence_sum do not match manifest")
    return GoodwareStage(stage_dir=stage_dir, source_dir=source_dir, manifest=manifest)

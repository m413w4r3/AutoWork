#!/usr/bin/env python3
"""Validate the M14 seed inputs and refuse unsafe partial writes.

The current production-state v5 importer requires the original production input
lineage to exist locally. This seed intentionally does not synthesize that
lineage from article text; it reports the incompatibility before any mutation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from cti_app.application.production_state import _validate_snapshot
from cti_app.config import Settings
from cti_app.infrastructure.database.session import create_postgres_engine
from sqlalchemy import inspect, text

FORMAT = "autowork.m14-source-manifest"
NAMESPACE = UUID("b5cde47f-2d30-4b0d-8e02-13cb97178ea1")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INVESTIGATION_TABLE_TERMS = (
    "investigation",
    "invocation",
    "observation",
    "finding",
    "sample",
    "rule",
    "detection_candidate",
)


def load_initial_hashes(path: Path) -> tuple[str, ...]:
    values = tuple(
        line.strip().lower() for line in path.read_text().splitlines() if line.strip()
    )
    if (
        len(values) != 15
        or len(set(values)) != 15
        or any(not SHA256_RE.fullmatch(x) for x in values)
    ):
        raise ValueError(
            "initial corpus hash list must contain 15 unique SHA-256 values"
        )
    return values


def synthesis_hash_coverage(
    state: dict[str, Any], hashes: tuple[str, ...]
) -> tuple[str, ...]:
    synthesis = json.dumps(
        state["artifacts"]["synthesis"]["canonical_content"],
        ensure_ascii=False,
        sort_keys=True,
    ).lower()
    return tuple(value for value in hashes if value in synthesis)


def source_document_references(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_document_id" and isinstance(item, str):
                found.add(item)
            elif key == "source_document_ids" and isinstance(item, list):
                found.update(entry for entry in item if isinstance(entry, str))
            found.update(source_document_references(item))
    elif isinstance(value, list):
        for item in value:
            found.update(source_document_references(item))
    return found


def validate_source_files(*, manifest: dict[str, Any], source_dir: Path) -> None:
    seen_ids: set[str] = set()
    for document in manifest["documents"]:
        if not isinstance(document, dict):
            raise TypeError("source manifest document entry is invalid")
        digest = str(document.get("sha256", ""))
        document_id = str(document.get("id", ""))
        size = document.get("size")
        if (
            not SHA256_RE.fullmatch(digest)
            or not document_id
            or document_id in seen_ids
        ):
            raise ValueError("source manifest identity or SHA-256 is invalid")
        if type(size) is not int or size < 0:
            raise ValueError("source manifest size is invalid")
        seen_ids.add(document_id)
        source_path = source_dir / digest
        if source_path.is_symlink() or not source_path.is_file():
            raise ValueError("source bytes file is missing or not a regular file")
        content = source_path.read_bytes()
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("source bytes do not match manifest size and SHA-256")


def validate_local_inputs(
    *, manifest: dict[str, Any], state_payload: dict[str, Any], hashes: tuple[str, ...]
) -> tuple[dict[str, Any], list[str]]:
    if manifest.get("format") != FORMAT or manifest.get("schema_version") != 1:
        raise ValueError("unsupported source manifest format")
    if not isinstance(manifest.get("documents"), list) or not manifest["documents"]:
        raise ValueError("source manifest has no documents")
    state = _validate_snapshot(state_payload)
    blockers: list[str] = []
    coverage = synthesis_hash_coverage(state_payload, hashes)
    if len(coverage) != len(hashes):
        blockers.append(
            f"synthesis_contains_{len(coverage)}_of_{len(hashes)}_initial_hashes"
        )

    if str(state.origin.subject_id) != str(manifest.get("source_subject_id")):
        blockers.append(
            "source_manifest_subject_id_differs_from_production_state_origin"
        )

    subject = manifest.get("subject")
    if (
        not isinstance(subject, dict)
        or not subject.get("title")
        or not subject.get("slug")
    ):
        raise ValueError("source manifest lacks subject title or slug")
    if state.origin.subject_title != subject["title"]:
        blockers.append("source_manifest_title_differs_from_production_state_origin")
    manifested_document_ids = {
        str(document.get("id"))
        for document in manifest["documents"]
        if isinstance(document, dict) and document.get("id")
    }
    referenced_document_ids = source_document_references(state_payload["artifacts"])
    if not referenced_document_ids.issubset(manifested_document_ids):
        blockers.append(
            "production_state_references_source_documents_absent_from_manifest"
        )
    # A fresh subject UUID cannot pass import_state's mandatory origin identity
    # check. This is reported here before creating an edition or source blob.
    candidate_subject_id = uuid5(
        NAMESPACE,
        f"m14-subject:{state.content_sha256}:{manifest['source_subject_id']}",
    )
    if candidate_subject_id == state.origin.subject_id:
        raise ValueError(
            "deterministic seed subject UUID unexpectedly equals the origin UUID"
        )
    blockers.append(
        "production_state_v5_requires_origin_subject_id_to_equal_target_subject_id"
    )
    blockers.append(
        "fresh_database_has_no_original_discovery_origin_or_active_snapshot"
    )
    blockers.append(
        "production_state_v5_does_not_export_the_full_production_input_snapshot"
    )
    blockers.append(
        "manual_archive_service_generates_new_document_ids_and_state_rewrite_is_unimplemented"
    )
    blockers.append(
        "production_state_import_creates_needs_review_run_pending_assembly_not_ready"
    )
    return state.model_dump(mode="json"), blockers


async def investigation_row_counts(settings: Settings) -> dict[str, int]:
    engine = create_postgres_engine(settings.postgres_dsn)
    counts: dict[str, int] = {}
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            tables = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names(
                    schema="public"
                )
            )
            preparer = connection.dialect.identifier_preparer
            for name in sorted(tables):
                lowered = name.casefold()
                if not any(term in lowered for term in INVESTIGATION_TABLE_TERMS):
                    continue
                quoted = preparer.quote(name)
                counts[name] = int(
                    await connection.scalar(
                        text(f"SELECT count(*) FROM public.{quoted}")
                    )
                    or 0
                )
    finally:
        await engine.dispose()
    return counts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--production-state", type=Path, required=True)
    parser.add_argument("--initial-hashes", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        state_payload = json.loads(args.production_state.read_text())
        hashes = load_initial_hashes(args.initial_hashes)
        validate_source_files(manifest=manifest, source_dir=args.manifest.parent)
        _state, blockers = validate_local_inputs(
            manifest=manifest, state_payload=state_payload, hashes=hashes
        )
        counts = await investigation_row_counts(Settings(_env_file=None))
        populated = {name: count for name, count in counts.items() if count}
        if populated:
            blockers.append("target_database_contains_investigation_or_sample_rows")
        result = {
            "status": "blocked" if blockers else "ready_for_seed",
            "dry_run": args.dry_run,
            "source_documents": len(manifest["documents"]),
            "initial_hashes": len(hashes),
            "synthesis_hashes_present": len(
                synthesis_hash_coverage(state_payload, hashes)
            ),
            "investigation_table_counts": counts,
            "blockers": blockers,
            "writes_performed": False,
        }
        print(json.dumps(result, sort_keys=True))
        return 2 if blockers else 0
    except Exception as exc:  # noqa: BLE001 - output is intentionally limited to error type.
        print(
            json.dumps({"status": "error", "error": type(exc).__name__}, sort_keys=True)
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

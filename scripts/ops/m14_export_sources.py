#!/usr/bin/env python3
"""Read-only export of one subject's source-document bytes and metadata."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.models.collection import SourceCollectionRow
from cti_app.infrastructure.database.models.core import (
    BlobRow,
    SourceDocumentRow,
    SubjectRow,
)
from cti_app.infrastructure.database.models.editions import EditionRow
from cti_app.infrastructure.database.session import create_postgres_engine
from minio import Minio
from sqlalchemy import select, text

DEFAULT_SUBJECT_ID = UUID("d22822d4-27ce-47d2-9cc5-f84cf312986a")
FORMAT = "autowork.m14-source-manifest"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_verified(path: Path, content: bytes, expected_sha256: str) -> None:
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("downloaded source SHA-256 mismatch")
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ValueError("refusing to replace a non-regular source export path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError(
                "existing source export file does not match its SHA-256 name"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as tmp:
        temp_path = Path(tmp.name)
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


async def export_sources(
    *,
    subject_id: UUID,
    output_dir: Path,
    settings: Settings,
    expected_count: int | None = 7,
) -> dict[str, Any]:
    """Export source-raw bytes after checking catalog and content digests."""
    engine = create_postgres_engine(settings.postgres_dsn)
    blob_store = MinioBlobStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        physical_bucket=settings.s3_bucket,
    )
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            subject = (
                await connection.execute(
                    select(SubjectRow).where(SubjectRow.id == subject_id)
                )
            ).scalar_one_or_none()
            if subject is None:
                raise ValueError("subject not found")
            edition = (
                await connection.execute(
                    select(EditionRow).where(EditionRow.id == subject.edition_id)
                )
            ).scalar_one_or_none()
            if edition is None:
                raise ValueError("subject edition not found")
            source_rows = list(
                (
                    await connection.execute(
                        select(SourceDocumentRow)
                        .where(SourceDocumentRow.subject_id == subject_id)
                        .order_by(SourceDocumentRow.created_at, SourceDocumentRow.id)
                    )
                ).scalars()
            )
            if expected_count is not None and len(source_rows) != expected_count:
                raise ValueError("unexpected source document count")
            blob_ids = {row.blob_id for row in source_rows}
            blob_rows = {
                row.id: row
                for row in (
                    await connection.execute(
                        select(BlobRow).where(BlobRow.id.in_(blob_ids))
                    )
                ).scalars()
            }
            collection_ids = {
                row.source_collection_id
                for row in source_rows
                if row.source_collection_id is not None
            }
            collections = (
                {
                    row.id: row
                    for row in (
                        await connection.execute(
                            select(SourceCollectionRow).where(
                                SourceCollectionRow.id.in_(collection_ids)
                            )
                        )
                    ).scalars()
                }
                if collection_ids
                else {}
            )

            documents: list[dict[str, Any]] = []
            for source in source_rows:
                blob = blob_rows.get(source.blob_id)
                if blob is None or blob.logical_bucket != "source-raw":
                    raise ValueError(
                        "source raw blob catalog entry is missing or misclassified"
                    )
                descriptor = BlobDescriptor(
                    sha256=blob.sha256,
                    size=blob.size,
                    mime_type=blob.mime_type,
                    logical_bucket=blob.logical_bucket,
                )
                content = await blob_store.read(descriptor, max_bytes=descriptor.size)
                if len(content) != descriptor.size:
                    raise ValueError("source raw blob size mismatch")
                _write_verified(
                    output_dir / descriptor.sha256, content, descriptor.sha256
                )
                collection = collections.get(source.source_collection_id)
                documents.append(
                    {
                        "id": str(source.id),
                        "original_name": source.original_name,
                        "origin": source.origin,
                        "url": source.final_url or source.origin,
                        "final_url": source.final_url,
                        "tlp": source.tlp,
                        "mime": source.declared_mime_type
                        or source.detected_mime_type
                        or blob.mime_type,
                        "stored_mime": blob.mime_type,
                        "sha256": descriptor.sha256,
                        "size": descriptor.size,
                        "acquired_at": source.acquired_at.isoformat(),
                        "collection_id": (
                            str(source.source_collection_id)
                            if source.source_collection_id
                            else None
                        ),
                        "source_candidate_id": (
                            str(source.source_candidate_id)
                            if source.source_candidate_id
                            else None
                        ),
                        "collection_relation": (
                            {
                                "origin_kind": collection.origin_kind,
                                "requested_url": collection.requested_url,
                                "canonical_url": collection.canonical_url,
                                "source_candidate_id": (
                                    str(collection.source_candidate_id)
                                    if collection.source_candidate_id
                                    else None
                                ),
                                "parent_source_collection_id": (
                                    str(collection.parent_source_collection_id)
                                    if collection.parent_source_collection_id
                                    else None
                                ),
                                "proposed_role": collection.proposed_role,
                                "relationship_status": collection.relationship_status,
                                "relationship_evidence": collection.relationship_evidence,
                            }
                            if collection
                            else None
                        ),
                    }
                )
            manifest = {
                "format": FORMAT,
                "schema_version": 1,
                "source_subject_id": str(subject_id),
                "subject": {
                    "id": str(subject.id),
                    "title": subject.title,
                    "slug": subject.slug,
                    "tlp": subject.tlp,
                },
                "edition": {
                    "country": edition.country,
                    "country_code": edition.country_code,
                    "period_start": edition.period_start.isoformat(),
                    "period_end": edition.period_end.isoformat(),
                    "tlp": edition.tlp,
                    "languages": edition.languages,
                },
                "documents": documents,
            }
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = output_dir / "sources_manifest.json"
        manifest_bytes = _json_bytes(manifest)
        if manifest_path.exists() and manifest_path.is_symlink():
            raise ValueError("refusing to replace a symlinked manifest")
        temp_manifest = manifest_path.with_name(f".{manifest_path.name}.tmp")
        temp_manifest.write_bytes(manifest_bytes)
        os.replace(temp_manifest, manifest_path)
        return manifest
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject-id", type=UUID, default=DEFAULT_SUBJECT_ID)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=7)
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    try:
        manifest = await export_sources(
            subject_id=args.subject_id,
            output_dir=args.output_dir,
            settings=Settings(_env_file=None),
            expected_count=args.expected_count,
        )
    except Exception as exc:  # noqa: BLE001 - output is intentionally limited to error type.
        print(json.dumps({"status": "error", "error": type(exc).__name__}))
        return 1
    print(
        json.dumps(
            {
                "status": "exported",
                "source_documents": len(manifest["documents"]),
                "manifest": str(args.output_dir / "sources_manifest.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

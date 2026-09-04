"""Content storage for production artifacts.

`ProductionArtifact` already carries `raw_blob_id`, `canonical_blob_id` and
`rendered_blob_id`; nothing was filling them, so every stage kept only counters
in `metadata` and the next stage had nothing to read back.

This store puts the real content in the blob catalog — which already
deduplicates by SHA-256 — and leaves `metadata` for counters alone.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from typing import Any
from uuid import UUID

from cti_app.application.blob_storage import BlobStorageUnavailableError
from cti_app.application.blobs import BlobCatalogService

# Production payloads are text; a publication or a reference report never approaches
# this, so it is a guard against reading a corrupted blob into memory.
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_REPAIR_EVIDENCE_BYTES = 12 * 1024 * 1024

_RAW_BUCKET = "production-artifacts-raw"
_CANONICAL_BUCKET = "production-artifacts-canonical"
_RENDERED_BUCKET = "production-artifacts-rendered"
_SOURCE_EXTRACTION_RAW_BUCKET = "source-extractions-raw"
_SOURCE_EXTRACTION_CANONICAL_BUCKET = "source-extractions-canonical"
_REPAIR_EVIDENCE_BUCKET = "production-repair-evidence"
REPAIR_EVIDENCE_BUCKET = _REPAIR_EVIDENCE_BUCKET


class ProductionReuseStorageUnavailableError(RuntimeError):
    """Canonical payload storage is unavailable and the stage must retry."""

    code = "production_reuse_storage_unavailable"
    retryable = True


class ProductionArtifactStore:
    """Reads and writes the three payloads of a production artifact."""

    def __init__(self, catalog: BlobCatalogService) -> None:
        self._catalog = catalog

    async def put_text(self, text: str, *, bucket: str) -> UUID:
        return await self.put_bytes(
            text.encode("utf-8"), bucket=bucket, mime_type="text/plain; charset=utf-8"
        )

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        record = await self._catalog.ingest(
            BytesIO(content), logical_bucket=bucket, mime_type=mime_type
        )
        return record.id

    async def put_json(self, payload: dict[str, Any], *, bucket: str) -> UUID:
        encoded = self.canonical_json_bytes(payload)
        record = await self._catalog.ingest(
            BytesIO(encoded),
            logical_bucket=bucket,
            mime_type="application/json",
        )
        return record.id

    @staticmethod
    def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
        """Canonical JSON bytes used for immutable, content-addressed payloads."""
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    async def put_canonical_json(self, payload: dict[str, Any], *, bucket: str) -> tuple[UUID, str]:
        encoded = self.canonical_json_bytes(payload)
        record = await self._catalog.ingest(
            BytesIO(encoded), logical_bucket=bucket, mime_type="application/json"
        )
        return record.id, hashlib.sha256(encoded).hexdigest()

    async def read_text(self, blob_id: UUID) -> str:
        return (await self.read_bytes(blob_id)).decode("utf-8")

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
        try:
            return await self._catalog.read(blob_id, max_bytes=max_bytes)
        except BlobStorageUnavailableError as exc:
            raise ProductionReuseStorageUnavailableError(str(exc)) from exc

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        payload = json.loads(await self.read_text(blob_id))
        if not isinstance(payload, dict):
            raise ValueError("Artifact payload is not a JSON object")
        return payload

    async def put_repair_evidence(self, payload: dict[str, Any]) -> UUID:
        """Store the complete, inert Q2 rejection pack under its own limit."""
        encoded = self.canonical_json_bytes(payload)
        if len(encoded) > MAX_REPAIR_EVIDENCE_BYTES:
            raise ValueError(f"Repair evidence pack exceeds {MAX_REPAIR_EVIDENCE_BYTES} bytes")
        record = await self._catalog.ingest(
            BytesIO(encoded),
            logical_bucket=_REPAIR_EVIDENCE_BUCKET,
            mime_type="application/json",
        )
        return record.id

    async def read_repair_evidence(self, blob_id: UUID) -> dict[str, Any]:
        """Read and validate one complete repair evidence pack."""
        payload = json.loads(
            (await self.read_bytes(blob_id, max_bytes=MAX_REPAIR_EVIDENCE_BYTES)).decode("utf-8")
        )
        if not isinstance(payload, dict):
            raise ValueError("Repair evidence payload is not a JSON object")
        if payload.get("schema_version") != "1" or not isinstance(payload.get("entries"), list):
            raise ValueError("Unsupported repair evidence pack")
        return payload

    async def store_stage_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        """Store whichever payloads a stage produced."""
        raw_id = await self.put_text(raw, bucket=_RAW_BUCKET) if raw else None
        canonical_id = (
            await self.put_json(canonical, bucket=_CANONICAL_BUCKET)
            if canonical is not None
            else None
        )
        rendered_id = await self.put_text(rendered, bucket=_RENDERED_BUCKET) if rendered else None
        return raw_id, canonical_id, rendered_id

    async def store_source_extraction_payloads(
        self, *, raw: str, canonical: dict[str, Any]
    ) -> tuple[UUID | None, UUID]:
        """Store source-centric Q2 payloads in buckets separate from artifacts."""
        raw_id = await self.put_text(raw, bucket=_SOURCE_EXTRACTION_RAW_BUCKET) if raw else None
        canonical_id = await self.put_json(canonical, bucket=_SOURCE_EXTRACTION_CANONICAL_BUCKET)
        return raw_id, canonical_id

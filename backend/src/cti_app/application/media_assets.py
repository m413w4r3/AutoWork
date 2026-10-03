"""Content-addressed storage for compiled diagrams and archived source figures."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from cti_app.application.diagram_compilation import (
    CompiledDiagram,
    DiagramCompilationError,
    DiagramCompiler,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.source_figure_inventory import (
    MAX_SOURCE_FIGURE_BYTES as _MAX_SOURCE_FIGURE_BYTES,
)
from cti_app.application.source_figure_inventory import (
    MAX_SOURCE_FIGURE_PIXELS,
    MAX_SOURCE_FIGURE_SIDE_LENGTH,
)
from cti_app.application.source_media_collection import (
    image_dimensions,
    image_dimensions_exceed_limits,
)
from cti_app.domain.errors import BlobIntegrityError, EntityNotFoundError
from cti_app.domain.media_assets import MediaAssetKind, MediaAssetManifest, media_asset_id
from cti_app.domain.production_editorial_enrichment import (
    DiagramSpecV1,
    ResolvedSourceFigureV1,
    SourceFigureDecision,
)

SOURCE_FIGURE_INGESTION_POLICY_VERSION = "source-figure-ingestion-v1"
MAX_SOURCE_FIGURE_BYTES = _MAX_SOURCE_FIGURE_BYTES
MAX_MEDIA_ASSET_READ_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DiagramCompilationRejection:
    diagram_key: str
    reason_code: str
    warning_code: str = "editorial_enrichment_diagram_render_failed"


@dataclass(frozen=True, slots=True)
class DiagramCompilationBatch:
    diagrams: tuple[DiagramSpecV1, ...]
    rejections: tuple[DiagramCompilationRejection, ...] = ()


_SVG_ROOT = re.compile(rb"(?:<\?xml[^>]*\?>\s*)?<svg(?:\s|>)", re.IGNORECASE)


class MediaAssetBlobStore(Protocol):
    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID: ...

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...


class MediaAssetStore:
    """Store immutable media bytes and their PostgreSQL manifest."""

    def __init__(
        self,
        blob_store: MediaAssetBlobStore,
        uow_factory: UnitOfWorkFactory,
    ) -> None:
        self._blob_store = blob_store
        self._uow_factory = uow_factory

    async def put(
        self,
        content: bytes,
        *,
        kind: MediaAssetKind,
        mime_type: str,
        source: str,
        policy_version: str,
        compiler_name: str | None = None,
        compiler_version: str | None = None,
        provenance: str | None = None,
        locator: dict[str, object] | None = None,
        decision: str | None = None,
    ) -> MediaAssetManifest:
        if not isinstance(content, bytes) or not content:
            raise ValueError("Media asset content must be non-empty bytes")
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise ValueError("Media asset MIME type must be non-empty")
        sha256 = hashlib.sha256(content).hexdigest()
        asset_id = media_asset_id(sha256, mime_type)
        candidate = MediaAssetManifest(
            asset_id=asset_id,
            kind=kind,
            blob_id=UUID(int=0),
            sha256=sha256,
            mime_type=mime_type,
            byte_size=len(content),
            source=source,
            compiler_name=compiler_name,
            compiler_version=compiler_version,
            policy_version=policy_version,
            provenance=provenance,
            locator=locator,
            decision=decision,
        )
        bucket = _media_asset_bucket(mime_type)
        blob_id = await self._blob_store.put_bytes(content, bucket=bucket, mime_type=mime_type)
        manifest = replace(candidate, blob_id=blob_id)
        async with self._uow_factory() as uow:
            stored = await uow.media_assets.add_if_absent(manifest)
            await uow.commit()
        if stored.kind is not kind:
            raise BlobIntegrityError("Media asset content address is already used by another kind")
        return stored

    async def get(self, asset_id: UUID) -> MediaAssetManifest | None:
        async with self._uow_factory() as uow:
            return await uow.media_assets.get(asset_id)

    async def read(self, asset_id: UUID, *, max_bytes: int = MAX_MEDIA_ASSET_READ_BYTES) -> bytes:
        manifest = await self.get(asset_id)
        if manifest is None:
            raise EntityNotFoundError(f"Media asset {asset_id} does not exist")
        content = await self._blob_store.read_bytes(manifest.blob_id, max_bytes=max_bytes)
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != manifest.byte_size or digest != manifest.sha256:
            raise BlobIntegrityError("Media asset bytes do not match their canonical manifest")
        return content

    async def store_diagram(self, compiled: CompiledDiagram, *, source: str) -> MediaAssetManifest:
        if compiled.media_type != "image/svg+xml":
            raise ValueError("Compiled diagrams must use the SVG MIME type")
        return await self.put(
            compiled.media_bytes,
            kind=MediaAssetKind.DIAGRAM_SVG,
            mime_type=compiled.media_type,
            source=source,
            compiler_name=compiled.compiler,
            compiler_version=compiled.compiler_version,
            policy_version=compiled.compiler_policy_version,
        )


class SourceFigureIngestor:
    """Validate local bytes for one accepted source figure before storing them."""

    def __init__(self, media_asset_store: MediaAssetStore) -> None:
        self._media_asset_store = media_asset_store

    async def ingest(self, figure: ResolvedSourceFigureV1, content: bytes) -> MediaAssetManifest:
        if figure.decision is not SourceFigureDecision.ACCEPTED:
            raise ValueError("Only accepted source figures can be ingested")
        if (
            figure.blob_id is None
            or figure.sha256 is None
            or figure.mime_type is None
            or figure.byte_size is None
        ):
            raise ValueError("Accepted source figure metadata is incomplete")
        if not isinstance(content, bytes) or not content:
            raise ValueError("Source figure content must be non-empty bytes")
        if len(content) > MAX_SOURCE_FIGURE_BYTES:
            raise ValueError("Source figure exceeds the ingestion byte limit")
        if len(content) != figure.byte_size:
            raise BlobIntegrityError("Source figure byte size does not match its archived metadata")
        if hashlib.sha256(content).hexdigest() != figure.sha256:
            raise BlobIntegrityError("Source figure SHA-256 does not match its archived metadata")
        if _sniff_source_figure_mime(content) != figure.mime_type:
            raise BlobIntegrityError("Source figure MIME type does not match its bytes")
        width, height = image_dimensions(content, figure.mime_type)
        if image_dimensions_exceed_limits(
            width,
            height,
            maximum_pixels=MAX_SOURCE_FIGURE_PIXELS,
            maximum_side_length=MAX_SOURCE_FIGURE_SIDE_LENGTH,
        ):
            raise ValueError("Source figure exceeds the ingestion dimension limit")
        return await self._media_asset_store.put(
            content,
            kind=MediaAssetKind.SOURCE_FIGURE,
            mime_type=figure.mime_type,
            source=figure.source,
            policy_version=SOURCE_FIGURE_INGESTION_POLICY_VERSION,
            provenance=figure.provenance,
            locator={
                "page": figure.locator.page,
                "section": figure.locator.section,
                "figure_label": figure.locator.figure_label,
                "original_asset_url": figure.locator.original_asset_url,
            },
            decision=figure.decision.value,
        )


async def compile_and_store_diagrams(
    diagrams: Sequence[DiagramSpecV1],
    *,
    compiler: DiagramCompiler,
    media_asset_store: MediaAssetStore,
    production_run_id: UUID,
) -> DiagramCompilationBatch:
    """Compile diagrams independently and retain typed failures for review."""
    compiled_diagrams: list[DiagramSpecV1] = []
    rejections: list[DiagramCompilationRejection] = []
    for diagram in diagrams:
        try:
            compiled = await compiler.compile(diagram)
        except DiagramCompilationError as exc:
            rejections.append(DiagramCompilationRejection(diagram.key, exc.code))
            continue
        if compiled.diagram_key != diagram.key:
            raise ValueError("Diagram compiler returned an asset for another diagram")
        manifest = await media_asset_store.store_diagram(
            compiled,
            source=f"production_run:{production_run_id}:diagram:{diagram.key}",
        )
        compiled_diagrams.append(replace(diagram, compiled_asset_id=manifest.asset_id))
    return DiagramCompilationBatch(tuple(compiled_diagrams), tuple(rejections))


def _media_asset_bucket(mime_type: str) -> str:
    mime_digest = hashlib.sha256(mime_type.encode("utf-8")).hexdigest()[:50]
    return f"media-assets-{mime_digest}"


def _sniff_source_figure_mime(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if _SVG_ROOT.match(content[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")):
        return "image/svg+xml"
    return None

"""Deterministic, network-free inventory of figures in archived source blobs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from io import BytesIO
from typing import cast
from urllib.parse import unquote_to_bytes, urljoin, urlsplit
from uuid import UUID

from cti_app.application.persistence import BlobRepository, SourceDocumentRepository
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.entities import SourceDocument
from cti_app.domain.production_editorial_enrichment import (
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    SourceFigureMimeType,
    source_figure_id,
)
from cti_app.domain.production_extraction import ProductionSourceExtractionV1

MAX_SOURCE_FIGURES = 64
MAX_SOURCE_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_SOURCE_FIGURE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_FIGURE_TOTAL_BYTES = 20 * 1024 * 1024

_ALLOWED_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/svg+xml", "image/webp", "image/gif"}
)
_DATA_URI_PREFIX = re.compile(r"^data:([^,]*?),(.*)$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True, slots=True)
class ArchivedFigureSource:
    source_document_id: UUID
    source_url: str
    mime_type: str
    blob_id: UUID
    sha256: str
    byte_size: int
    content: bytes


@dataclass(frozen=True, slots=True)
class ArchivedFigureAsset:
    source_document_id: UUID
    source_url: str | None
    blob_id: UUID
    sha256: str
    mime_type: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class SourceFigureInventoryResult:
    figures: tuple[ResolvedSourceFigureV1, ...]
    truncated: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def accepted(self) -> tuple[ResolvedSourceFigureV1, ...]:
        return tuple(
            figure for figure in self.figures if figure.decision is SourceFigureDecision.ACCEPTED
        )

    def content_hash(self) -> str:
        payload = {
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "figures": [
                {
                    "figure_id": str(figure.figure_id),
                    "blob_id": str(figure.blob_id) if figure.blob_id is not None else None,
                    "sha256": figure.sha256,
                    "mime_type": figure.mime_type,
                    "byte_size": figure.byte_size,
                    "source_document_id": str(figure.source_document_id),
                    "source": figure.source,
                    "provenance": figure.provenance,
                    "locator": {
                        "page": figure.locator.page,
                        "section": figure.locator.section,
                        "figure_label": figure.locator.figure_label,
                        "original_asset_url": figure.locator.original_asset_url,
                    },
                    "decision": figure.decision.value,
                    "decision_reason": figure.decision_reason,
                }
                for figure in self.figures
            ],
        }
        encoded = ProductionArtifactStore.canonical_json_bytes(payload)
        return hashlib.sha256(encoded).hexdigest()

    def functional_hash(self) -> str | None:
        if not self.figures and not self.truncated and not self.warnings:
            return None
        return self.content_hash()


class _ImageElementParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[tuple[str | None, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "img":
            return
        values = {name.casefold(): value for name, value in attrs}
        self.images.append((values.get("src"), values.get("alt") or values.get("title")))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


class SourceFigureInventory:
    """Find source images without fetching URLs or creating media blobs."""

    def __init__(
        self,
        *,
        max_figures: int = MAX_SOURCE_FIGURES,
        max_source_bytes: int = MAX_SOURCE_DOCUMENT_BYTES,
        max_figure_bytes: int = MAX_SOURCE_FIGURE_BYTES,
        max_total_figure_bytes: int = MAX_SOURCE_FIGURE_TOTAL_BYTES,
    ) -> None:
        for name, value in (
            ("max_figures", max_figures),
            ("max_source_bytes", max_source_bytes),
            ("max_figure_bytes", max_figure_bytes),
            ("max_total_figure_bytes", max_total_figure_bytes),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._max_figures = max_figures
        self._max_source_bytes = max_source_bytes
        self._max_figure_bytes = max_figure_bytes
        self._max_total_figure_bytes = max_total_figure_bytes

    def inventory(
        self,
        sources: tuple[ArchivedFigureSource, ...],
        assets: tuple[ArchivedFigureAsset, ...],
    ) -> SourceFigureInventoryResult:
        source_by_id = {source.source_document_id: source for source in sources}
        if len(source_by_id) != len(sources):
            raise ValueError("An archived source document may only appear once")
        ordered_sources = tuple(
            sorted(sources, key=lambda item: (item.source_url, item.source_document_id.hex))
        )
        assets_by_url: dict[str, ArchivedFigureAsset] = {}
        assets_by_hash: dict[str, ArchivedFigureAsset] = {}
        for asset in sorted(
            assets,
            key=lambda item: (
                item.source_url or "",
                item.sha256,
                item.source_document_id.hex,
                item.blob_id.hex,
            ),
        ):
            assets_by_hash.setdefault(asset.sha256, asset)
            normalized_url = _canonical_http_url(asset.source_url)
            if normalized_url is not None:
                assets_by_url.setdefault(normalized_url, asset)

        found: list[ResolvedSourceFigureV1] = []
        indices_by_hash: dict[str, int] = {}
        seen_locations: set[tuple[UUID, str]] = set()
        total_figure_bytes = 0
        truncated = False
        warnings: set[str] = set()

        def add(
            *,
            source: ArchivedFigureSource,
            locator: SourceFigureLocatorV1,
            asset_url: str | None,
            provenance: str,
            decision: SourceFigureDecision,
            reason: str,
            blob: ArchivedFigureAsset | None = None,
            sha256: str | None = None,
            mime_type: str | None = None,
            byte_size: int | None = None,
        ) -> bool:
            nonlocal total_figure_bytes, truncated
            if sha256 is not None:
                duplicate_index = indices_by_hash.get(sha256)
                if duplicate_index is not None:
                    previous = found[duplicate_index]
                    if not (
                        decision is SourceFigureDecision.ACCEPTED
                        and previous.decision is not SourceFigureDecision.ACCEPTED
                        and previous.decision_reason
                        not in {
                            "figure_exceeds_byte_limit",
                            "inventory_exceeds_total_byte_limit",
                        }
                    ):
                        return True
            else:
                location = (source.source_document_id, asset_url or locator.figure_label or "")
                if location in seen_locations:
                    return True
            duplicate = sha256 is not None and sha256 in indices_by_hash
            if not duplicate and len(found) >= self._max_figures:
                truncated = True
                return False
            if byte_size is not None:
                if byte_size > self._max_figure_bytes:
                    decision = SourceFigureDecision.REJECTED
                    reason = "figure_exceeds_byte_limit"
                    blob = None
                elif _total_limit_exceeded(
                    duplicate, total_figure_bytes, byte_size, self._max_total_figure_bytes
                ):
                    decision = SourceFigureDecision.REJECTED
                    reason = "inventory_exceeds_total_byte_limit"
                    blob = None
                else:
                    if not duplicate:
                        total_figure_bytes += byte_size
            selected_locator = SourceFigureLocatorV1(
                page=locator.page,
                section=locator.section,
                figure_label=locator.figure_label,
                original_asset_url=asset_url,
            )
            figure = ResolvedSourceFigureV1(
                figure_id=source_figure_id(
                    source_document_id=source.source_document_id,
                    sha256=sha256,
                    source=source.source_url,
                    locator=selected_locator,
                ),
                blob_id=blob.blob_id if blob is not None else None,
                sha256=blob.sha256 if blob is not None else sha256,
                mime_type=_allowed_mime(blob.mime_type)
                if blob is not None
                else _allowed_mime(mime_type),
                byte_size=blob.byte_size if blob is not None else byte_size,
                source_document_id=source.source_document_id,
                source=source.source_url,
                provenance=provenance,
                locator=selected_locator,
                decision=decision,
                decision_reason=reason,
            )
            if sha256 is not None:
                if duplicate:
                    found[indices_by_hash[sha256]] = figure
                else:
                    indices_by_hash[sha256] = len(found)
                    found.append(figure)
            else:
                seen_locations.add(
                    (source.source_document_id, asset_url or locator.figure_label or "")
                )
                found.append(figure)
            return True

        for source in ordered_sources:
            if (
                len(source.content) > self._max_source_bytes
                or source.byte_size > self._max_source_bytes
            ):
                warnings.add("source_figure_source_exceeds_byte_limit")
                continue
            mime_type = _normalized_mime(source.mime_type)
            if mime_type in {"text/html", "application/xhtml+xml"}:
                try:
                    parser = _ImageElementParser()
                    parser.feed(source.content.decode("utf-8", errors="replace"))
                    parser.close()
                except Exception:
                    warnings.add("source_figure_html_parse_failed")
                    continue
                for index, (src, label) in enumerate(parser.images, start=1):
                    asset_url: str | None = None
                    locator = SourceFigureLocatorV1(
                        figure_label=(label or f"HTML image {index}").strip(),
                    )
                    if src is None or not src.strip():
                        if not add(
                            source=source,
                            locator=locator,
                            asset_url=None,
                            provenance=(
                                f"img element in archived document {source.source_document_id}"
                            ),
                            decision=SourceFigureDecision.REJECTED,
                            reason="image_source_missing",
                        ):
                            break
                        continue
                    if src.lstrip().casefold().startswith("data:"):
                        observation = _data_uri(src.strip(), self._max_figure_bytes)
                        if observation.error is not None:
                            if not add(
                                source=source,
                                locator=locator,
                                asset_url=None,
                                provenance=(
                                    f"data URI in archived document {source.source_document_id}"
                                ),
                                decision=SourceFigureDecision.REJECTED,
                                reason=observation.error,
                                sha256=observation.sha256,
                                mime_type=observation.mime_type,
                                byte_size=observation.byte_size,
                            ):
                                break
                            continue
                        assert observation.sha256 is not None
                        data_locator = SourceFigureLocatorV1(
                            figure_label=locator.figure_label,
                            original_asset_url=(
                                f"data:{observation.mime_type or 'image/unknown'};"
                                f"sha256={observation.sha256}"
                            ),
                        )
                        local_asset = assets_by_hash.get(observation.sha256)
                        decision, reason = _resolve_observed_bytes(observation, local_asset)
                        if not add(
                            source=source,
                            locator=data_locator,
                            asset_url=data_locator.original_asset_url,
                            provenance=f"data URI in archived document {source.source_document_id}",
                            decision=decision,
                            reason=reason,
                            blob=local_asset if decision is SourceFigureDecision.ACCEPTED else None,
                            sha256=observation.sha256,
                            mime_type=observation.mime_type,
                            byte_size=observation.byte_size,
                        ):
                            break
                        continue
                    parsed = urlsplit(src.strip())
                    if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
                        if not add(
                            source=source,
                            locator=locator,
                            asset_url=None,
                            provenance=(
                                f"image reference in archived document {source.source_document_id}"
                            ),
                            decision=SourceFigureDecision.REJECTED,
                            reason="unsupported_image_source_scheme",
                        ):
                            break
                        continue
                    asset_url = _canonical_http_url(urljoin(source.source_url, src.strip()))
                    if asset_url is None:
                        if not add(
                            source=source,
                            locator=locator,
                            asset_url=None,
                            provenance=(
                                f"image reference in archived document {source.source_document_id}"
                            ),
                            decision=SourceFigureDecision.REJECTED,
                            reason="invalid_image_source_url",
                        ):
                            break
                        continue
                    matched_asset = assets_by_url.get(asset_url)
                    decision, reason = _resolve_archived_asset(
                        matched_asset, self._max_figure_bytes
                    )
                    if not add(
                        source=source,
                        locator=locator,
                        asset_url=asset_url,
                        provenance=(
                            f"image URL in archived document {source.source_document_id}; "
                            f"local archive match {matched_asset.source_document_id}"
                            if matched_asset is not None
                            else f"image URL in archived document {source.source_document_id}"
                        ),
                        decision=decision,
                        reason=reason,
                        blob=matched_asset if decision is SourceFigureDecision.ACCEPTED else None,
                        sha256=matched_asset.sha256 if matched_asset is not None else None,
                        mime_type=matched_asset.mime_type if matched_asset is not None else None,
                        byte_size=matched_asset.byte_size if matched_asset is not None else None,
                    ):
                        break
            elif mime_type == "application/pdf":
                pdf_images, pdf_warning = _pdf_images(source)
                if pdf_warning is not None:
                    warnings.add(pdf_warning)
                for page, name, content, extraction_error in pdf_images:
                    locator = SourceFigureLocatorV1(
                        page=page,
                        figure_label=name or f"PDF page {page} image",
                    )
                    digest = hashlib.sha256(content).hexdigest() if content is not None else None
                    image_mime = _sniff_mime(content) if content is not None else None
                    matched_pdf_asset = assets_by_hash.get(digest) if digest is not None else None
                    decision, reason = _resolve_observed_bytes(
                        _ObservedImage(
                            digest,
                            image_mime,
                            len(content) if content is not None else None,
                            extraction_error,
                        ),
                        matched_pdf_asset,
                    )
                    if not add(
                        source=source,
                        locator=locator,
                        asset_url=None,
                        provenance=(
                            f"embedded image on PDF page {page} of {source.source_document_id}"
                        ),
                        decision=decision,
                        reason=reason,
                        blob=matched_pdf_asset
                        if decision is SourceFigureDecision.ACCEPTED
                        else None,
                        sha256=digest,
                        mime_type=image_mime,
                        byte_size=len(content) if content is not None else None,
                    ):
                        break
            if truncated:
                break

        ordered = tuple(
            sorted(
                found,
                key=lambda item: (
                    item.source_document_id.hex,
                    item.locator.page or 0,
                    item.locator.original_asset_url or "",
                    item.sha256 or "",
                    item.figure_id.hex,
                ),
            )
        )
        return SourceFigureInventoryResult(
            figures=ordered,
            truncated=truncated,
            warnings=tuple(sorted(warnings)),
        )


@dataclass(frozen=True, slots=True)
class _ObservedImage:
    sha256: str | None
    mime_type: str | None
    byte_size: int | None
    error: str | None


def _data_uri(value: str, max_bytes: int) -> _ObservedImage:
    match = _DATA_URI_PREFIX.fullmatch(value)
    if match is None:
        return _ObservedImage(None, None, None, "invalid_data_uri")
    metadata, encoded = match.groups()
    metadata_parts = metadata.split(";") if metadata else []
    declared_mime = _normalized_mime(metadata_parts[0] if metadata_parts else "text/plain")
    is_base64 = any(part.casefold() == "base64" for part in metadata_parts[1:])
    if len(encoded) > max_bytes * (4 if is_base64 else 3) + 8:
        return _ObservedImage(None, _allowed_mime(declared_mime), None, "figure_exceeds_byte_limit")
    try:
        content = (
            base64.b64decode(encoded, validate=True) if is_base64 else unquote_to_bytes(encoded)
        )
    except (ValueError, binascii.Error):
        return _ObservedImage(None, _allowed_mime(declared_mime), None, "invalid_data_uri_payload")
    digest = hashlib.sha256(content).hexdigest()
    actual_mime = _sniff_mime(content)
    if len(content) > max_bytes:
        return _ObservedImage(
            digest, _allowed_mime(actual_mime), len(content), "figure_exceeds_byte_limit"
        )
    if actual_mime is None or actual_mime not in _ALLOWED_MIME_TYPES:
        return _ObservedImage(digest, None, len(content), "unsupported_image_mime_type")
    if declared_mime != actual_mime:
        return _ObservedImage(digest, actual_mime, len(content), "data_uri_mime_mismatch")
    return _ObservedImage(digest, actual_mime, len(content), None)


def _resolve_observed_bytes(
    observed: _ObservedImage,
    asset: ArchivedFigureAsset | None,
) -> tuple[SourceFigureDecision, str]:
    if observed.error is not None:
        return SourceFigureDecision.REJECTED, observed.error
    if observed.mime_type not in _ALLOWED_MIME_TYPES:
        return SourceFigureDecision.REJECTED, "unsupported_image_mime_type"
    if asset is None:
        return SourceFigureDecision.PENDING, "image_bytes_not_in_blob_store"
    if (
        asset.sha256 != observed.sha256
        or _allowed_mime(asset.mime_type) != observed.mime_type
        or asset.byte_size != observed.byte_size
    ):
        return SourceFigureDecision.REJECTED, "archived_blob_metadata_mismatch"
    return _resolve_archived_asset(asset, MAX_SOURCE_FIGURE_BYTES)


def _total_limit_exceeded(
    duplicate: bool, current_size: int, candidate_size: int, maximum: int
) -> bool:
    return not duplicate and current_size + candidate_size > maximum


def _resolve_archived_asset(
    asset: ArchivedFigureAsset | None, max_figure_bytes: int
) -> tuple[SourceFigureDecision, str]:
    if asset is None:
        return SourceFigureDecision.PENDING, "image_not_in_local_archive"
    if _allowed_mime(asset.mime_type) is None:
        return SourceFigureDecision.REJECTED, "unsupported_image_mime_type"
    if asset.byte_size > max_figure_bytes:
        return SourceFigureDecision.REJECTED, "figure_exceeds_byte_limit"
    return SourceFigureDecision.ACCEPTED, "matched_archived_blob"


def _pdf_images(
    source: ArchivedFigureSource,
) -> tuple[tuple[tuple[int, str | None, bytes | None, str | None], ...], str | None]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return (), "source_figure_pdf_extraction_unavailable"
    images: list[tuple[int, str | None, bytes | None, str | None]] = []
    try:
        reader = PdfReader(BytesIO(source.content), strict=False)
        for page_number, page in enumerate(reader.pages, start=1):
            for image in page.images:
                content = image.data
                name = str(getattr(image, "name", "") or "") or None
                images.append((page_number, name, content, None))
    except Exception:
        return tuple(images), "source_figure_pdf_parse_failed"
    return tuple(images), None


def _sniff_mime(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    sample = content[:1024].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if re.match(rb"(?:<\?xml[^>]*\?>\s*)?<svg(?:\s|>)", sample):
        return "image/svg+xml"
    return None


def _normalized_mime(value: str) -> str:
    return value.split(";", 1)[0].strip().casefold()


def _allowed_mime(value: str | None) -> SourceFigureMimeType | None:
    if value is None:
        return None
    normalized = _normalized_mime(value)
    return cast(SourceFigureMimeType, normalized) if normalized in _ALLOWED_MIME_TYPES else None


def _canonical_http_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return canonicalize_http_url(value)
    except (AttributeError, ValueError):
        return None


async def load_archived_source_figure_inventory(
    *,
    subject_id: UUID,
    extraction_sources: Sequence[ProductionSourceExtractionV1],
    source_document_repository: SourceDocumentRepository,
    blob_repository: BlobRepository,
    artifact_store: ProductionArtifactStore,
    inventory: SourceFigureInventory | None = None,
) -> SourceFigureInventoryResult:
    """Read only source and image blobs already catalogued for this subject."""
    scanner = inventory or SourceFigureInventory()
    documents = await source_document_repository.list_for_subject(subject_id)
    documents_by_id = {document.id: document for document in documents}
    descriptor_by_blob_id: dict[UUID, BlobDescriptor | None] = {}

    async def descriptor_for(blob_id: UUID) -> BlobDescriptor | None:
        if blob_id not in descriptor_by_blob_id:
            record = await blob_repository.get(blob_id)
            descriptor_by_blob_id[blob_id] = record.descriptor if record is not None else None
        return descriptor_by_blob_id[blob_id]

    def effective_blob_id(document: SourceDocument) -> UUID:
        return document.decoded_blob_id or document.blob_id

    archived_assets: list[ArchivedFigureAsset] = []
    for document in sorted(documents, key=lambda item: item.id.hex):
        blob_id = effective_blob_id(document)
        descriptor = await descriptor_for(blob_id)
        if descriptor is None:
            continue
        mime_type = _normalized_mime(document.detected_mime_type or descriptor.mime_type)
        if not mime_type.startswith("image/"):
            continue
        archived_assets.append(
            ArchivedFigureAsset(
                source_document_id=document.id,
                source_url=document.final_url,
                blob_id=blob_id,
                sha256=descriptor.sha256,
                mime_type=mime_type,
                byte_size=descriptor.size,
            )
        )

    archived_sources: list[ArchivedFigureSource] = []
    loader_warnings: set[str] = set()
    for source in sorted(
        extraction_sources,
        key=lambda item: (item.canonical_url, item.source_document_id.hex),
    ):
        source_document_id = source.source_document_id
        source_document = documents_by_id.get(source_document_id)
        if source_document is None or source_document.subject_id != subject_id:
            loader_warnings.add("source_figure_source_document_unavailable")
            continue
        blob_id = effective_blob_id(source_document)
        descriptor = await descriptor_for(blob_id)
        if descriptor is None:
            loader_warnings.add("source_figure_source_blob_unavailable")
            continue
        mime_type = _normalized_mime(source_document.detected_mime_type or descriptor.mime_type)
        if mime_type not in {"text/html", "application/xhtml+xml", "application/pdf"}:
            continue
        content = b""
        if descriptor.size <= scanner._max_source_bytes:
            content = await artifact_store.read_bytes(blob_id, max_bytes=scanner._max_source_bytes)
        archived_sources.append(
            ArchivedFigureSource(
                source_document_id=source_document_id,
                source_url=source.canonical_url,
                mime_type=mime_type,
                blob_id=blob_id,
                sha256=descriptor.sha256,
                byte_size=descriptor.size,
                content=content,
            )
        )

    result = scanner.inventory(tuple(archived_sources), tuple(archived_assets))
    if not loader_warnings:
        return result
    return replace(result, warnings=tuple(sorted(set(result.warnings) | loader_warnings)))

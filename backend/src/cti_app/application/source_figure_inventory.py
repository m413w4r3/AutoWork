"""Deterministic source-figure inventory backed by archived media blobs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import unquote_to_bytes
from uuid import UUID

from cti_app.application.persistence import BlobRepository, SourceDocumentRepository
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.source_media_collection import (
    SOURCE_MEDIA_MAX_PIXELS,
    SOURCE_MEDIA_MAX_SIDE_LENGTH,
    image_dimensions,
    image_dimensions_exceed_limits,
)
from cti_app.application.source_media_extraction import (
    SourceMediaObservation,
    extract_source_media_observations,
)
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.entities import SourceDocument
from cti_app.domain.media_assets import SUPPORTED_MEDIA_MIME_TYPES
from cti_app.domain.production_editorial_enrichment import (
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    SourceFigureMimeType,
    source_figure_id,
)
from cti_app.domain.production_extraction import ProductionSourceExtractionV1
from cti_app.domain.source_media import (
    SourceMediaReasonCode,
    SourceMediaRecord,
    SourceMediaStatus,
)

MAX_SOURCE_FIGURES = 64
MAX_SOURCE_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_SOURCE_FIGURE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_FIGURE_TOTAL_BYTES = 20 * 1024 * 1024
MAX_SOURCE_FIGURE_PIXELS = SOURCE_MEDIA_MAX_PIXELS
MAX_SOURCE_FIGURE_SIDE_LENGTH = SOURCE_MEDIA_MAX_SIDE_LENGTH

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
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class SourceFigureCatalogMetadata:
    """Archived source context used only to describe a figure to the model."""

    alt_text: str | None = None
    caption_text: str | None = None
    nearby_heading_text: str | None = None
    anchor: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class SourceFigureInventoryResult:
    figures: tuple[ResolvedSourceFigureV1, ...]
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    policy_sha256: str | None = None
    catalog_metadata: Mapping[UUID, SourceFigureCatalogMetadata] = field(default_factory=dict)

    @property
    def accepted(self) -> tuple[ResolvedSourceFigureV1, ...]:
        return tuple(
            figure for figure in self.figures if figure.decision is SourceFigureDecision.ACCEPTED
        )

    def content_hash(self) -> str:
        def catalog_metadata_for(figure: ResolvedSourceFigureV1) -> dict[str, int | str | None]:
            metadata = self.catalog_metadata.get(figure.figure_id, SourceFigureCatalogMetadata())
            return {
                "alt_text": metadata.alt_text,
                "caption_text": metadata.caption_text,
                "nearby_heading_text": metadata.nearby_heading_text,
                "anchor": metadata.anchor,
                "width": metadata.width,
                "height": metadata.height,
            }

        payload = {
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "policy_sha256": self.policy_sha256,
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
                    "catalog_metadata": catalog_metadata_for(figure),
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
    """Resolve source observations against locally archived media blobs."""

    def __init__(
        self,
        *,
        max_figures: int = MAX_SOURCE_FIGURES,
        max_source_bytes: int = MAX_SOURCE_DOCUMENT_BYTES,
        max_figure_bytes: int = MAX_SOURCE_FIGURE_BYTES,
        max_total_figure_bytes: int = MAX_SOURCE_FIGURE_TOTAL_BYTES,
        max_figure_pixels: int = MAX_SOURCE_FIGURE_PIXELS,
        max_figure_side_length: int = MAX_SOURCE_FIGURE_SIDE_LENGTH,
    ) -> None:
        for name, value in (
            ("max_figures", max_figures),
            ("max_source_bytes", max_source_bytes),
            ("max_figure_bytes", max_figure_bytes),
            ("max_total_figure_bytes", max_total_figure_bytes),
            ("max_figure_pixels", max_figure_pixels),
            ("max_figure_side_length", max_figure_side_length),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._max_figures = max_figures
        self._max_source_bytes = max_source_bytes
        self._max_figure_bytes = max_figure_bytes
        self._max_total_figure_bytes = max_total_figure_bytes
        self._max_figure_pixels = max_figure_pixels
        self._max_figure_side_length = max_figure_side_length

    def inventory(
        self,
        sources: tuple[ArchivedFigureSource, ...],
        assets: tuple[ArchivedFigureAsset, ...],
        *,
        media_candidates: tuple[SourceMediaRecord, ...] = (),
        policy_sha256: str | None = None,
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
        catalog_metadata: dict[UUID, SourceFigureCatalogMetadata] = {}

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
            media_record: SourceMediaRecord | None = None,
            width: int | None = None,
            height: int | None = None,
            alt_text: str | None = None,
            caption_text: str | None = None,
            nearby_heading_text: str | None = None,
            anchor: str | None = None,
        ) -> bool:
            nonlocal total_figure_bytes, truncated
            observed_width = width
            observed_height = height
            if media_record is not None:
                if observed_width is None:
                    observed_width = media_record.width
                if observed_height is None:
                    observed_height = media_record.height
            if image_dimensions_exceed_limits(
                observed_width,
                observed_height,
                maximum_pixels=self._max_figure_pixels,
                maximum_side_length=self._max_figure_side_length,
            ):
                decision = SourceFigureDecision.REJECTED
                reason = SourceMediaReasonCode.IMAGE_TOO_LARGE_DIMENSIONS.value
                blob = None
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
                            SourceMediaReasonCode.IMAGE_TOO_LARGE_DIMENSIONS.value,
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
            catalog_metadata[figure.figure_id] = SourceFigureCatalogMetadata(
                alt_text=(media_record.alt_text if media_record is not None else None) or alt_text,
                caption_text=(media_record.caption_text if media_record is not None else None)
                or caption_text,
                nearby_heading_text=(
                    media_record.nearby_heading_text if media_record is not None else None
                )
                or nearby_heading_text,
                anchor=(media_record.anchor if media_record is not None else None) or anchor,
                width=observed_width,
                height=observed_height,
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

        observations_by_source: dict[UUID, list[SourceMediaObservation]] = {}
        for observation in extract_source_media_observations(tuple(ordered_sources)):
            observations_by_source.setdefault(observation.source_document_id, []).append(
                observation
            )
        media_by_location = {
            (record.source_document_id, record.dom_locator): record
            for record in media_candidates
            if record.dom_locator is not None
        }

        for source in ordered_sources:
            if (
                len(source.content) > self._max_source_bytes
                or source.byte_size > self._max_source_bytes
            ):
                warnings.add("source_figure_source_exceeds_byte_limit")
                continue
            mime_type = _normalized_mime(source.mime_type)
            if mime_type == "application/pdf":
                _pdf_extractions, pdf_warning = _pdf_images(source)
                if pdf_warning is not None:
                    warnings.add(pdf_warning)
            for index, observation in enumerate(
                observations_by_source.get(source.source_document_id, ()), start=1
            ):
                if observation.is_page_excerpt:
                    warnings.add("source_figure_pdf_page_excerpt_needed")
                    continue
                label = observation.alt_text or (
                    observation.dom_locator.rsplit(":", 1)[-1]
                    if mime_type == "application/pdf"
                    else f"HTML image {index}"
                )
                asset_url = observation.requested_url
                if asset_url and asset_url.casefold().startswith("data:"):
                    embedded = observation.image_bytes
                    digest = hashlib.sha256(embedded).hexdigest() if embedded is not None else None
                    image_mime = _sniff_mime(embedded) if embedded is not None else None
                    safe_asset_url = (
                        f"data:{image_mime or 'image/unknown'};sha256={digest}"
                        if digest is not None
                        else "data:image/unknown;sha256=unavailable"
                    )
                    asset_url = safe_asset_url
                locator = SourceFigureLocatorV1(
                    page=observation.page,
                    section=observation.nearby_heading_text,
                    figure_label=label.strip() if label else f"HTML image {index}",
                    original_asset_url=asset_url,
                )
                media_record = media_by_location.get(
                    (source.source_document_id, observation.dom_locator)
                )
                provenance = _media_provenance(source, observation)
                if observation.pre_exclusion_reason is not None:
                    if not add(
                        source=source,
                        locator=locator,
                        asset_url=asset_url,
                        provenance=provenance,
                        decision=SourceFigureDecision.REJECTED,
                        reason=observation.pre_exclusion_reason.value,
                        media_record=media_record,
                        alt_text=observation.alt_text,
                        caption_text=observation.caption_text,
                        nearby_heading_text=observation.nearby_heading_text,
                        anchor=observation.anchor,
                    ):
                        break
                    continue
                if media_record is not None and media_record.status in {
                    SourceMediaStatus.EXCLUDED_BY_RULE,
                    SourceMediaStatus.COLLECTION_FAILED,
                }:
                    if not add(
                        source=source,
                        locator=locator,
                        asset_url=asset_url,
                        provenance=provenance,
                        decision=SourceFigureDecision.REJECTED,
                        reason=media_record.reason_code.value,
                        sha256=media_record.sha256,
                        mime_type=media_record.mime_type,
                        byte_size=media_record.byte_size,
                        media_record=media_record,
                        alt_text=observation.alt_text,
                        caption_text=observation.caption_text,
                        nearby_heading_text=observation.nearby_heading_text,
                        anchor=observation.anchor,
                    ):
                        break
                    continue
                if observation.image_bytes is not None:
                    content = observation.image_bytes
                    digest = hashlib.sha256(content).hexdigest()
                    image_mime = _sniff_mime(content)
                    width, height = image_dimensions(content, image_mime)
                    observed = _ObservedImage(digest, image_mime, len(content), None, width, height)
                    matched = assets_by_hash.get(digest)
                    decision, reason = _resolve_observed_bytes(
                        observed,
                        matched,
                        self._max_figure_bytes,
                        self._max_figure_pixels,
                        self._max_figure_side_length,
                    )
                    if (
                        decision is SourceFigureDecision.ACCEPTED
                        and media_record is not None
                        and media_record.status is SourceMediaStatus.ACCEPTED_FOR_REVIEW
                    ):
                        reason = "accepted_for_review"
                    if not add(
                        source=source,
                        locator=locator,
                        asset_url=asset_url,
                        provenance=provenance,
                        decision=decision,
                        reason=reason,
                        blob=matched if decision is SourceFigureDecision.ACCEPTED else None,
                        sha256=digest,
                        mime_type=image_mime,
                        byte_size=len(content),
                        width=width,
                        height=height,
                        media_record=media_record,
                        alt_text=observation.alt_text,
                        caption_text=observation.caption_text,
                        nearby_heading_text=observation.nearby_heading_text,
                        anchor=observation.anchor,
                    ):
                        break
                    continue
                if asset_url is None:
                    if not add(
                        source=source,
                        locator=locator,
                        asset_url=None,
                        provenance=provenance,
                        decision=SourceFigureDecision.REJECTED,
                        reason="invalid_image_source_url",
                        media_record=media_record,
                        alt_text=observation.alt_text,
                        caption_text=observation.caption_text,
                        nearby_heading_text=observation.nearby_heading_text,
                        anchor=observation.anchor,
                    ):
                        break
                    continue
                matched_asset = assets_by_url.get(_canonical_http_url(asset_url) or "")
                decision, reason = _resolve_archived_asset(
                    matched_asset,
                    self._max_figure_bytes,
                    self._max_figure_pixels,
                    self._max_figure_side_length,
                )
                if (
                    decision is SourceFigureDecision.ACCEPTED
                    and media_record is not None
                    and media_record.status is SourceMediaStatus.ACCEPTED_FOR_REVIEW
                ):
                    reason = (
                        "accepted_for_review"
                        if matched_asset is not None
                        else "image_bytes_not_in_blob_store"
                    )
                    decision = (
                        SourceFigureDecision.ACCEPTED
                        if matched_asset is not None
                        else SourceFigureDecision.PENDING
                    )
                if not add(
                    source=source,
                    locator=locator,
                    asset_url=asset_url,
                    provenance=provenance,
                    decision=decision,
                    reason=reason,
                    blob=matched_asset if decision is SourceFigureDecision.ACCEPTED else None,
                    sha256=matched_asset.sha256 if matched_asset is not None else None,
                    mime_type=matched_asset.mime_type if matched_asset is not None else None,
                    byte_size=matched_asset.byte_size if matched_asset is not None else None,
                    media_record=media_record,
                    alt_text=observation.alt_text,
                    caption_text=observation.caption_text,
                    nearby_heading_text=observation.nearby_heading_text,
                    anchor=observation.anchor,
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
            policy_sha256=policy_sha256,
            catalog_metadata=catalog_metadata,
        )


@dataclass(frozen=True, slots=True)
class _ObservedImage:
    sha256: str | None
    mime_type: str | None
    byte_size: int | None
    error: str | None
    width: int | None = None
    height: int | None = None


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
    if actual_mime is None or actual_mime not in SUPPORTED_MEDIA_MIME_TYPES:
        return _ObservedImage(digest, None, len(content), "unsupported_image_mime_type")
    width, height = image_dimensions(content, actual_mime)
    if image_dimensions_exceed_limits(
        width,
        height,
        maximum_pixels=MAX_SOURCE_FIGURE_PIXELS,
        maximum_side_length=MAX_SOURCE_FIGURE_SIDE_LENGTH,
    ):
        return _ObservedImage(
            digest,
            actual_mime,
            len(content),
            SourceMediaReasonCode.IMAGE_TOO_LARGE_DIMENSIONS.value,
            width,
            height,
        )
    if declared_mime != actual_mime:
        return _ObservedImage(
            digest, actual_mime, len(content), "data_uri_mime_mismatch", width, height
        )
    return _ObservedImage(digest, actual_mime, len(content), None, width, height)


def _resolve_observed_bytes(
    observed: _ObservedImage,
    asset: ArchivedFigureAsset | None,
    max_figure_bytes: int = MAX_SOURCE_FIGURE_BYTES,
    max_figure_pixels: int = MAX_SOURCE_FIGURE_PIXELS,
    max_figure_side_length: int = MAX_SOURCE_FIGURE_SIDE_LENGTH,
) -> tuple[SourceFigureDecision, str]:
    if observed.error is not None:
        return SourceFigureDecision.REJECTED, observed.error
    if image_dimensions_exceed_limits(
        observed.width if observed.width is not None else (asset.width if asset else None),
        observed.height if observed.height is not None else (asset.height if asset else None),
        maximum_pixels=max_figure_pixels,
        maximum_side_length=max_figure_side_length,
    ):
        return SourceFigureDecision.REJECTED, SourceMediaReasonCode.IMAGE_TOO_LARGE_DIMENSIONS.value
    if observed.mime_type not in SUPPORTED_MEDIA_MIME_TYPES:
        return SourceFigureDecision.REJECTED, "unsupported_image_mime_type"
    if asset is None:
        return SourceFigureDecision.PENDING, "image_bytes_not_in_blob_store"
    if (
        asset.sha256 != observed.sha256
        or _allowed_mime(asset.mime_type) != observed.mime_type
        or asset.byte_size != observed.byte_size
    ):
        return SourceFigureDecision.REJECTED, "archived_blob_metadata_mismatch"
    return _resolve_archived_asset(
        asset,
        max_figure_bytes,
        max_figure_pixels,
        max_figure_side_length,
    )


def _total_limit_exceeded(
    duplicate: bool, current_size: int, candidate_size: int, maximum: int
) -> bool:
    return not duplicate and current_size + candidate_size > maximum


def _resolve_archived_asset(
    asset: ArchivedFigureAsset | None,
    max_figure_bytes: int,
    max_figure_pixels: int = MAX_SOURCE_FIGURE_PIXELS,
    max_figure_side_length: int = MAX_SOURCE_FIGURE_SIDE_LENGTH,
) -> tuple[SourceFigureDecision, str]:
    if asset is None:
        return SourceFigureDecision.PENDING, "image_not_in_local_archive"
    if _allowed_mime(asset.mime_type) is None:
        return SourceFigureDecision.REJECTED, "unsupported_image_mime_type"
    if asset.byte_size > max_figure_bytes:
        return SourceFigureDecision.REJECTED, "figure_exceeds_byte_limit"
    if image_dimensions_exceed_limits(
        asset.width,
        asset.height,
        maximum_pixels=max_figure_pixels,
        maximum_side_length=max_figure_side_length,
    ):
        return SourceFigureDecision.REJECTED, SourceMediaReasonCode.IMAGE_TOO_LARGE_DIMENSIONS.value
    return SourceFigureDecision.ACCEPTED, "matched_archived_blob"


def _pdf_images(
    source: ArchivedFigureSource,
) -> tuple[tuple[tuple[int, str | None, bytes | None, str | None], ...], str | None]:
    observations = extract_source_media_observations((source,))
    return (
        tuple(
            (
                observation.page or 1,
                observation.dom_locator.rsplit(":", 1)[-1],
                observation.image_bytes,
                observation.extraction_error,
            )
            for observation in observations
            if not observation.is_page_excerpt
        ),
        None,
    )


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


def _media_provenance(source: ArchivedFigureSource, observation: SourceMediaObservation) -> str:
    details = [
        f"archived source document {source.source_document_id}",
        observation.dom_locator,
    ]
    for label, value in (
        ("alt", observation.alt_text),
        ("caption", observation.caption_text),
        ("heading", observation.nearby_heading_text),
        ("anchor", observation.anchor),
    ):
        if value:
            details.append(f"{label}: {value}")
    return "; ".join(details)


def _normalized_mime(value: str) -> str:
    return value.split(";", 1)[0].strip().casefold()


def _allowed_mime(value: str | None) -> SourceFigureMimeType | None:
    if value is None:
        return None
    normalized = _normalized_mime(value)
    return normalized if normalized in SUPPORTED_MEDIA_MIME_TYPES else None


def _canonical_http_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return canonicalize_http_url(value)
    except (AttributeError, ValueError):
        return None


class SourceMediaArchiver(Protocol):
    @property
    def policy_sha256(self) -> str: ...

    async def collect(
        self,
        subject_id: UUID,
        sources: tuple[ArchivedFigureSource, ...],
    ) -> tuple[SourceMediaRecord, ...]: ...


async def load_archived_source_figure_inventory(
    *,
    subject_id: UUID,
    extraction_sources: Sequence[ProductionSourceExtractionV1],
    source_document_repository: SourceDocumentRepository,
    blob_repository: BlobRepository,
    artifact_store: ProductionArtifactStore,
    inventory: SourceFigureInventory | None = None,
    media_archiver: SourceMediaArchiver | None = None,
) -> SourceFigureInventoryResult:
    """Build a figure inventory and collect unresolved local-source media."""
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
        width: int | None = None
        height: int | None = None
        if descriptor.size <= MAX_SOURCE_FIGURE_BYTES:
            content = await artifact_store.read_bytes(blob_id, max_bytes=MAX_SOURCE_FIGURE_BYTES)
            width, height = image_dimensions(content, mime_type)
        archived_assets.append(
            ArchivedFigureAsset(
                source_document_id=document.id,
                source_url=document.final_url,
                blob_id=blob_id,
                sha256=descriptor.sha256,
                mime_type=mime_type,
                byte_size=descriptor.size,
                width=width,
                height=height,
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
                source_url=source_document.final_url or source.canonical_url,
                mime_type=mime_type,
                blob_id=blob_id,
                sha256=descriptor.sha256,
                byte_size=descriptor.size,
                content=content,
            )
        )

    media_candidates: tuple[SourceMediaRecord, ...] = ()
    policy_sha256: str | None = None
    if media_archiver is not None and archived_sources:
        media_candidates = await media_archiver.collect(subject_id, tuple(archived_sources))
        policy_sha256 = media_archiver.policy_sha256
        for candidate in media_candidates:
            if (
                candidate.blob_id is None
                or candidate.sha256 is None
                or candidate.mime_type is None
                or candidate.byte_size is None
            ):
                continue
            archived_assets.append(
                ArchivedFigureAsset(
                    source_document_id=candidate.source_document_id,
                    source_url=candidate.requested_url,
                    blob_id=candidate.blob_id,
                    sha256=candidate.sha256,
                    mime_type=candidate.mime_type,
                    byte_size=candidate.byte_size,
                    width=candidate.width,
                    height=candidate.height,
                )
            )

    result = scanner.inventory(
        tuple(archived_sources),
        tuple(archived_assets),
        media_candidates=media_candidates,
        policy_sha256=policy_sha256,
    )
    if not loader_warnings:
        return result
    return replace(result, warnings=tuple(sorted(set(result.warnings) | loader_warnings)))

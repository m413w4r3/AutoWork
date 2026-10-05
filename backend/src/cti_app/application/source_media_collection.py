from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import zlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionError,
    SafeHttpCollector,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.source_media_extraction import (
    SourceMediaObservation,
    SourceMediaSource,
    extract_source_media_observations,
)
from cti_app.domain.media_assets import SUPPORTED_MEDIA_MIME_TYPES
from cti_app.domain.source_media import (
    SourceMediaReasonCode,
    SourceMediaRecord,
    SourceMediaStatus,
)

SOURCE_MEDIA_POLICY_VERSION = "source-media-collection-v4-aspect-ratio-warning"
SOURCE_MEDIA_MAX_BYTES = 5 * 1024 * 1024
SOURCE_MEDIA_MAX_PIXELS = 40_000_000
SOURCE_MEDIA_MAX_SIDE_LENGTH = 10_000
SOURCE_MEDIA_BUCKET = "source-media-candidates"


class _SharedRecordFields(TypedDict):
    sha256: str
    mime_type: str | None
    width: int | None
    height: int | None
    byte_size: int
    final_url: str | None
    diagnostics: dict[str, object]
    collected_at: datetime


@dataclass(frozen=True, slots=True)
class SourceMediaPolicy:
    version: str = SOURCE_MEDIA_POLICY_VERSION
    minimum_width: int = 160
    minimum_height: int = 100
    minimum_bytes: int = 2 * 1024
    maximum_bytes: int = SOURCE_MEDIA_MAX_BYTES
    maximum_pixels: int = SOURCE_MEDIA_MAX_PIXELS
    maximum_side_length: int = SOURCE_MEDIA_MAX_SIDE_LENGTH
    perceptual_hamming_distance: int = 4
    boilerplate_pattern: str = (
        r"(?:logo|favicon|brand|avatar|social|share|modal|close|profile|author-photo|"
        r"menu|navbar|navigation|breadcrumb|search|subscribe|header|footer|banner|"
        r"icon|pixel|beacon|tracking|spacer)"
    )
    landmark_pattern: str = (
        r"(?:nav|menu|header|footer|sidebar|social|related|recent|popular|recommended)"
    )
    tracking_pattern: str = r"(?:pixel|tracking|beacon|spacer|1x1)(?:[._/?=-]|$)"
    related_content_heading_pattern: str = (
        r"(?:related|recent|popular|recommended|you\s+may\s+also\s+like|read\s+more|"
        r"more\s+stories|latest\s+articles)"
    )
    maximum_aspect_ratio: float = 4.0

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("Source media policy version must be non-empty")
        for name in (
            "minimum_width",
            "minimum_height",
            "minimum_bytes",
            "maximum_bytes",
            "maximum_pixels",
            "maximum_side_length",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.perceptual_hamming_distance < 0:
            raise ValueError("perceptual_hamming_distance must not be negative")
        if not math.isfinite(self.maximum_aspect_ratio) or self.maximum_aspect_ratio <= 1:
            raise ValueError("maximum_aspect_ratio must be greater than one")
        re.compile(self.boilerplate_pattern)
        re.compile(self.landmark_pattern)
        re.compile(self.tracking_pattern)
        re.compile(self.related_content_heading_pattern)

    @property
    def sha256(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class SourceMediaArchiveService:
    """Collect and archive deterministic candidates from already archived sources."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        collector: SafeHttpCollector,
        blob_store: BlobStore,
        *,
        policy: SourceMediaPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._collector = collector
        self._catalog = BlobCatalogService(blob_store, uow_factory)
        self.policy = policy or SourceMediaPolicy()

    @property
    def policy_sha256(self) -> str:
        return self.policy.sha256

    async def collect(
        self,
        subject_id: UUID,
        sources: tuple[SourceMediaSource, ...],
    ) -> tuple[SourceMediaRecord, ...]:
        observations = extract_source_media_observations(
            sources,
            boilerplate_pattern=self.policy.boilerplate_pattern,
            landmark_pattern=self.policy.landmark_pattern,
            tracking_pattern=self.policy.tracking_pattern,
            related_content_heading_pattern=self.policy.related_content_heading_pattern,
        )
        async with self._uow_factory() as uow:
            existing = tuple(
                await uow.source_media_candidates.list_for_subject_policy(
                    subject_id, self.policy.sha256
                )
            )
        by_id = {record.id: record for record in existing}
        resolved: list[SourceMediaRecord] = []
        exact_hashes: dict[str, SourceMediaRecord] = {
            record.sha256: record
            for record in existing
            if record.sha256 and record.status is SourceMediaStatus.ACCEPTED_FOR_REVIEW
        }
        perceptual_hashes: list[tuple[str, SourceMediaRecord]] = [
            (record.perceptual_hash, record)
            for record in existing
            if record.perceptual_hash and record.status is SourceMediaStatus.ACCEPTED_FOR_REVIEW
        ]
        for observation in observations:
            candidate_id = _candidate_id(observation, self.policy.sha256)
            prior = by_id.get(candidate_id)
            if prior is not None:
                resolved.append(prior)
                continue
            if observation.is_page_excerpt:
                record = self._base_record(
                    subject_id,
                    observation,
                    candidate_id,
                    status=SourceMediaStatus.PAGE_EXCERPT_NEEDED,
                    reason=SourceMediaReasonCode.PDF_PAGE_EXCERPT_NEEDED,
                )
                resolved.append(record)
                continue
            if observation.extraction_error is not None:
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.COLLECTION_FAILED,
                        reason=SourceMediaReasonCode.PDF_IMAGE_EXTRACTION_FAILED,
                        diagnostics={"extraction_error": observation.extraction_error},
                    )
                )
                continue
            if observation.pre_exclusion_reason is not None:
                record = self._base_record(
                    subject_id,
                    observation,
                    candidate_id,
                    status=SourceMediaStatus.EXCLUDED_BY_RULE,
                    reason=observation.pre_exclusion_reason,
                )
                resolved.append(record)
                continue
            if not observation.original_url and observation.image_bytes is None:
                record = self._base_record(
                    subject_id,
                    observation,
                    candidate_id,
                    status=SourceMediaStatus.EXCLUDED_BY_RULE,
                    reason=SourceMediaReasonCode.MISSING_SOURCE_URL,
                )
                resolved.append(record)
                continue
            if observation.original_url and observation.requested_url is None:
                record = self._base_record(
                    subject_id,
                    observation,
                    candidate_id,
                    status=SourceMediaStatus.EXCLUDED_BY_RULE,
                    reason=SourceMediaReasonCode.INVALID_SOURCE_URL,
                )
                resolved.append(record)
                continue
            if observation.requested_url and observation.requested_url.casefold().startswith(
                "data:"
            ):
                content = observation.image_bytes
                response_metadata: dict[str, object] = {
                    "source": "archived_data_uri",
                    "requested_url": _redact_data_uri(observation.requested_url, content),
                    "collected_at": datetime.now(UTC).isoformat(),
                }
                final_url = None
                collected_at = datetime.now(UTC)
            elif observation.image_bytes is not None:
                content = observation.image_bytes
                response_metadata = {
                    "source": "embedded_pdf_image",
                    "collected_at": datetime.now(UTC).isoformat(),
                }
                final_url = None
                collected_at = datetime.now(UTC)
            else:
                try:
                    assert observation.requested_url is not None
                    response = await self._collector.fetch(
                        observation.requested_url,
                        allow_images=True,
                    )
                except CollectionError as exc:
                    reason = _collection_reason(exc)
                    resolved.append(
                        self._base_record(
                            subject_id,
                            observation,
                            candidate_id,
                            status=SourceMediaStatus.COLLECTION_FAILED,
                            reason=reason,
                            final_url=exc.final_url,
                            diagnostics=_error_diagnostics(exc),
                        )
                    )
                    continue
                content = response.decoded_body
                response_metadata = _response_diagnostics(response)
                final_url = response.final_url
                collected_at = response.acquired_at
            if content is None:
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.COLLECTION_FAILED,
                        reason=SourceMediaReasonCode.COLLECTION_FAILED,
                        diagnostics={**response_metadata, "failure": "missing_image_bytes"},
                        collected_at=collected_at,
                    )
                )
                continue
            digest = hashlib.sha256(content).hexdigest()
            mime_type = _sniff_image_mime(content)
            width, height = image_dimensions(content, mime_type)
            aspect_ratio_warning = (
                width is not None
                and height is not None
                and max(width / height, height / width) > self.policy.maximum_aspect_ratio
            )
            if aspect_ratio_warning:
                response_metadata = {
                    **response_metadata,
                    "aspect_ratio_warning": SourceMediaReasonCode.EXTREME_ASPECT_RATIO.value,
                }
            shared_fields: _SharedRecordFields = dict(
                sha256=digest,
                mime_type=mime_type,
                width=width,
                height=height,
                byte_size=len(content),
                final_url=final_url,
                diagnostics=response_metadata,
                collected_at=collected_at,
            )
            if len(content) > self.policy.maximum_bytes:
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.EXCLUDED_BY_RULE,
                        reason=SourceMediaReasonCode.EXCEEDS_MAXIMUM_BYTES,
                        **shared_fields,
                    )
                )
                continue
            if mime_type not in SUPPORTED_MEDIA_MIME_TYPES:
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.COLLECTION_FAILED,
                        reason=SourceMediaReasonCode.NOT_AN_IMAGE
                        if mime_type is None
                        else SourceMediaReasonCode.UNSUPPORTED_IMAGE_TYPE,
                        **shared_fields,
                    )
                )
                continue
            if image_dimensions_exceed_limits(
                width,
                height,
                maximum_pixels=self.policy.maximum_pixels,
                maximum_side_length=self.policy.maximum_side_length,
            ):
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.EXCLUDED_BY_RULE,
                        reason=SourceMediaReasonCode.IMAGE_TOO_LARGE_DIMENSIONS,
                        **shared_fields,
                    )
                )
                continue
            if width == 1 or height == 1:
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.EXCLUDED_BY_RULE,
                        reason=SourceMediaReasonCode.TRACKING_PIXEL,
                        **shared_fields,
                    )
                )
                continue
            if (width is not None and width < self.policy.minimum_width) or (
                height is not None and height < self.policy.minimum_height
            ):
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.EXCLUDED_BY_RULE,
                        reason=SourceMediaReasonCode.BELOW_MINIMUM_DIMENSIONS,
                        **shared_fields,
                    )
                )
                continue
            if len(content) < self.policy.minimum_bytes:
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.EXCLUDED_BY_RULE,
                        reason=SourceMediaReasonCode.BELOW_MINIMUM_BYTES,
                        **shared_fields,
                    )
                )
                continue
            try:
                blob = await self._catalog.ingest(
                    BytesIO(content), logical_bucket=SOURCE_MEDIA_BUCKET, mime_type=mime_type
                )
            except Exception:
                resolved.append(
                    self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.COLLECTION_FAILED,
                        reason=SourceMediaReasonCode.COLLECTION_FAILED,
                        **shared_fields,
                    )
                )
                continue
            perceptual_hash = image_perceptual_hash(content, mime_type)
            duplicate = exact_hashes.get(digest)
            if duplicate is not None:
                record = self._base_record(
                    subject_id,
                    observation,
                    candidate_id,
                    status=SourceMediaStatus.EXCLUDED_BY_RULE,
                    reason=SourceMediaReasonCode.DUPLICATE_EXACT_HASH,
                    blob_id=blob.id,
                    perceptual_hash=perceptual_hash,
                    **shared_fields,
                )
            else:
                near_duplicate = next(
                    (
                        previous
                        for previous_hash, previous in perceptual_hashes
                        if _hamming_distance(perceptual_hash, previous_hash)
                        <= self.policy.perceptual_hamming_distance
                    ),
                    None,
                )
                if near_duplicate is not None:
                    record = self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.EXCLUDED_BY_RULE,
                        reason=SourceMediaReasonCode.DUPLICATE_PERCEPTUAL_HASH,
                        blob_id=blob.id,
                        perceptual_hash=perceptual_hash,
                        **shared_fields,
                    )
                else:
                    record = self._base_record(
                        subject_id,
                        observation,
                        candidate_id,
                        status=SourceMediaStatus.ACCEPTED_FOR_REVIEW,
                        reason=SourceMediaReasonCode.ELIGIBLE_FOR_REVIEW,
                        blob_id=blob.id,
                        perceptual_hash=perceptual_hash,
                        **shared_fields,
                    )
                    exact_hashes[digest] = record
                    if perceptual_hash is not None:
                        perceptual_hashes.append((perceptual_hash, record))
            resolved.append(record)
        async with self._uow_factory() as uow:
            stored: list[SourceMediaRecord] = []
            for record in resolved:
                stored.append(await uow.source_media_candidates.add_if_absent(record))
            await uow.commit()
        return tuple(stored)

    def _base_record(
        self,
        subject_id: UUID,
        observation: SourceMediaObservation,
        candidate_id: UUID,
        *,
        status: SourceMediaStatus,
        reason: SourceMediaReasonCode,
        blob_id: UUID | None = None,
        sha256: str | None = None,
        mime_type: str | None = None,
        width: int | None = None,
        height: int | None = None,
        byte_size: int | None = None,
        final_url: str | None = None,
        diagnostics: dict[str, object] | None = None,
        perceptual_hash: str | None = None,
        collected_at: datetime | None = None,
    ) -> SourceMediaRecord:
        original_url = observation.original_url
        if original_url and original_url.casefold().startswith("data:"):
            original_url = _redact_data_uri(original_url, observation.image_bytes)
        requested_url = observation.requested_url
        if requested_url and requested_url.casefold().startswith("data:"):
            requested_url = _redact_data_uri(requested_url, observation.image_bytes)
        return SourceMediaRecord(
            id=candidate_id,
            subject_id=subject_id,
            source_document_id=observation.source_document_id,
            policy_version=self.policy.version,
            policy_sha256=self.policy.sha256,
            status=status,
            reason_code=reason,
            blob_id=blob_id,
            sha256=sha256,
            mime_type=mime_type,
            width=width,
            height=height,
            byte_size=byte_size,
            page=observation.page,
            page_bbox=observation.page_bbox,
            anchor=observation.anchor,
            dom_locator=observation.dom_locator,
            original_url=original_url,
            requested_url=requested_url,
            final_url=final_url,
            alt_text=observation.alt_text,
            caption_text=observation.caption_text,
            nearby_heading_text=observation.nearby_heading_text,
            collection_diagnostics=dict(diagnostics or {}),
            perceptual_hash=perceptual_hash,
            collected_at=collected_at or datetime.now(UTC),
        )


def _candidate_id(observation: SourceMediaObservation, policy_sha256: str) -> UUID:
    identity = "|".join(
        (
            "source-media-v1",
            str(observation.source_document_id),
            observation.source_sha256,
            observation.dom_locator,
            observation.requested_url or hashlib.sha256(observation.image_bytes or b"").hexdigest(),
            policy_sha256,
        )
    )
    return uuid5(NAMESPACE_URL, identity)


def _collection_reason(error: CollectionError) -> SourceMediaReasonCode:
    reason = getattr(error.reason_code, "value", str(error.reason_code))
    if reason == "unsafe_destination":
        return SourceMediaReasonCode.UNSAFE_DESTINATION
    if reason == "size_limit":
        return SourceMediaReasonCode.COLLECTION_SIZE_LIMIT
    if getattr(error.outcome, "value", "") == "unavailable":
        return SourceMediaReasonCode.COLLECTION_UNAVAILABLE
    return SourceMediaReasonCode.COLLECTION_FAILED


def _error_diagnostics(error: CollectionError) -> dict[str, object]:
    return {
        "collector_reason_code": getattr(error.reason_code, "value", str(error.reason_code)),
        "outcome": getattr(error.outcome, "value", str(error.outcome)),
        "retryable": error.retryable,
        "final_url": error.final_url,
        "redirect_chain": list(error.redirect_chain),
        "http_status": error.http_status,
        "encoded_size": error.encoded_size,
        "detected_content_type": error.detected_content_type,
    }


def _response_diagnostics(response: CollectedResponse) -> dict[str, object]:
    return {
        "requested_url": response.requested_url,
        "final_url": response.final_url,
        "redirect_chain": list(response.redirect_chain),
        "http_status": response.status,
        "declared_content_type": response.declared_content_type,
        "detected_content_type": response.detected_content_type.value,
        "encoded_size": response.encoded_size,
        "encoded_sha256": response.encoded_sha256,
        "decoded_size": response.decoded_size,
        "decoded_sha256": response.decoded_sha256,
        "content_encoding": response.content_encoding,
        "collected_at": response.acquired_at.isoformat(),
    }


def _sniff_image_mime(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if re.match(rb"(?:<\?xml[^>]*\?>\s*)?<svg(?:\s|>)", content[:1024].lstrip(), re.I):
        return "image/svg+xml"
    return None


def image_dimensions(content: bytes, mime_type: str | None) -> tuple[int | None, int | None]:
    try:
        if mime_type == "image/png" and len(content) >= 24 and content[12:16] == b"IHDR":
            return struct.unpack(">II", content[16:24])
        if mime_type == "image/gif" and len(content) >= 10:
            return struct.unpack("<HH", content[6:10])
        if mime_type == "image/jpeg":
            return _jpeg_dimensions(content)
        if mime_type == "image/webp":
            return _webp_dimensions(content)
        if mime_type == "image/svg+xml":
            return _svg_dimensions(content)
    except (ValueError, IndexError, struct.error):
        return None, None
    return None, None


def image_dimensions_exceed_limits(
    width: int | None,
    height: int | None,
    *,
    maximum_pixels: int = SOURCE_MEDIA_MAX_PIXELS,
    maximum_side_length: int = SOURCE_MEDIA_MAX_SIDE_LENGTH,
) -> bool:
    return (
        (width is not None and width > maximum_side_length)
        or (height is not None and height > maximum_side_length)
        or (width is not None and height is not None and width * height > maximum_pixels)
    )


def _jpeg_dimensions(content: bytes) -> tuple[int | None, int | None]:
    position = 2
    while position + 4 <= len(content) and content[position] == 0xFF:
        marker = content[position + 1]
        position += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(content):
            break
        length = int.from_bytes(content[position : position + 2], "big")
        if length < 2 or position + length > len(content):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(content[position + 3 : position + 5], "big")
            width = int.from_bytes(content[position + 5 : position + 7], "big")
            return width, height
        position += length
    return None, None


def _webp_dimensions(content: bytes) -> tuple[int | None, int | None]:
    if len(content) < 30:
        return None, None
    chunk = content[12:16]
    if chunk == b"VP8X":
        return (
            int.from_bytes(content[24:27], "little") + 1,
            int.from_bytes(content[27:30], "little") + 1,
        )
    if chunk == b"VP8L" and len(content) >= 25:
        bits = int.from_bytes(content[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None, None


def _svg_dimensions(content: bytes) -> tuple[int | None, int | None]:
    root = re.search(rb"<svg\b([^>]*)>", content[:4096], re.I)
    if root is None:
        return None, None
    attributes = root.group(1)
    width = re.search(rb"\bwidth\s*=\s*['\"](\d+(?:\.\d+)?)", attributes, re.I)
    height = re.search(rb"\bheight\s*=\s*['\"](\d+(?:\.\d+)?)", attributes, re.I)
    if width and height:
        return max(1, int(float(width.group(1)))), max(1, int(float(height.group(1))))
    viewbox = re.search(
        rb"\bviewBox\s*=\s*['\"]\s*[-+\d.eE]+[ ,]+[-+\d.eE]+[ ,]+([\d.eE+-]+)[ ,]+([\d.eE+-]+)",
        attributes,
        re.I,
    )
    if viewbox:
        return max(1, int(float(viewbox.group(1)))), max(1, int(float(viewbox.group(2))))
    return None, None


def image_perceptual_hash(content: bytes, mime_type: str | None) -> str | None:
    """Compute a small horizontal difference hash for safe, non-interlaced PNGs."""
    if mime_type != "image/png":
        return None
    try:
        return _png_dhash(content)
    except (ValueError, zlib.error, struct.error):
        return None


def _png_dhash(content: bytes) -> str | None:
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    offset = 8
    width: int | None = None
    height: int | None = None
    bit_depth: int | None = None
    color_type: int | None = None
    interlace: int | None = None
    compressed = bytearray()
    palette = b""
    while offset + 12 <= len(content):
        length = int.from_bytes(content[offset : offset + 4], "big")
        chunk_type = content[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(content):
            return None
        chunk = content[start:end]
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
        elif chunk_type == b"IDAT":
            compressed.extend(chunk)
        elif chunk_type == b"PLTE":
            palette = chunk
        elif chunk_type == b"IEND":
            break
        offset = end + 4
    if (
        width is None
        or height is None
        or bit_depth != 8
        or interlace != 0
        or width < 1
        or height < 1
        or width * height > 24_000_000
    ):
        return None
    assert width is not None and height is not None and color_type is not None
    channels: int | None = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None or (color_type == 3 and not palette):
        return None
    stride = width * channels
    expected = height * (stride + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(compressed), expected + 1)
    if len(raw) != expected or decoder.unconsumed_tail:
        return None
    rows: list[bytes] = []
    prior = bytearray(stride)
    cursor = 0
    for _row_index in range(height):
        filter_kind = raw[cursor]
        cursor += 1
        filtered = raw[cursor : cursor + stride]
        cursor += stride
        row = bytearray(stride)
        for index, value in enumerate(filtered):
            left = row[index - channels] if index >= channels else 0
            up = prior[index]
            upper_left = prior[index - channels] if index >= channels else 0
            if filter_kind == 0:
                predictor = 0
            elif filter_kind == 1:
                predictor = left
            elif filter_kind == 2:
                predictor = up
            elif filter_kind == 3:
                predictor = (left + up) // 2
            elif filter_kind == 4:
                predictor = _paeth(left, up, upper_left)
            else:
                return None
            row[index] = (value + predictor) & 0xFF
        rows.append(bytes(row))
        prior = row

    def luminance(x: int, y: int) -> int:
        row = rows[min(height - 1, y * height // 8)]
        px = min(width - 1, x * width // 9)
        position = px * channels
        if color_type == 0:
            return row[position]
        if color_type == 4:
            return row[position]
        if color_type == 3:
            palette_position = row[position] * 3
            if palette_position + 2 >= len(palette):
                return 0
            red, green, blue = palette[palette_position : palette_position + 3]
        else:
            red, green, blue = row[position : position + 3]
        return (299 * red + 587 * green + 114 * blue) // 1000

    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | int(luminance(x, y) > luminance(x + 1, y))
    return f"{bits:016x}"


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left


def _hamming_distance(left: str | None, right: str | None) -> int:
    if left is None or right is None:
        return 65
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _redact_data_uri(value: str, content: bytes | None) -> str:
    digest = hashlib.sha256(content).hexdigest() if content is not None else "unavailable"
    mime = value[5:].split(";", 1)[0].split(",", 1)[0].strip() or "application/octet-stream"
    return f"data:{mime};sha256={digest}"

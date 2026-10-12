from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.pdf_mapping import build_mapping_projection, mapping_cache_key
from cti_app.domain.edition_render import EDITION_RENDERER_VERSION
from cti_app.domain.pdf_inspection import (
    PdfDiagnosticClass,
    PdfInspectionAvailability,
    PdfInspectionErrorCode,
    PdfInspectionProfile,
    PdfMappingStatus,
    PdfRasterStatus,
)

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_WARNING_COUNT = 20
MAX_WARNING_LENGTH = 512
PREVIEW_DPI = 120
PREVIEW_MAX_EDGE_PX = 1600
THUMBNAIL_MAX_EDGE_PX = 480
LOW_OCCUPANCY_THRESHOLD = 0.35
INSPECTION_POLICY_VERSION = "pdf-inspection-policy-v1"
DIAGNOSTICS_VERSION = "pdf-diagnostics-v2"
BLOCK_TOO_SMALL_MIN_WIDTH_POINTS = 120.0
BLOCK_TOO_SMALL_MIN_HEIGHT_POINTS = 72.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAGE_REQUEST_SEMAPHORE = asyncio.Semaphore(2)


class PdfInspectionError(Exception):
    def __init__(self, code: PdfInspectionErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class PdfRenderReference:
    id: UUID
    kind: str
    status: str
    input_hash: str
    renderer_version: str
    template_sha256: str
    compiler_version: str
    font_bundle_version: str
    output_blob_id: UUID | None
    output_sha256: str | None
    output_byte_size: int | None
    source: dict[str, Any]
    renderer: str = "typst"
    template_version: str = "unknown"
    compiler: str = "typst"
    source_blob_id: UUID | None = None
    render_data_blob_id: UUID | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class PdfResolvedProvenance:
    render: PdfRenderReference | None
    source: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PdfInspectionSource:
    content: bytes
    provenances: tuple[PdfResolvedProvenance, ...]
    preview: bool


@dataclass(frozen=True, slots=True)
class PdfInspectionResult:
    readable: bool
    page_count: int
    page_width_points: float | None
    page_height_points: float | None
    encrypted: bool
    parser_warnings: tuple[str, ...]
    non_embedded_fonts: tuple[str, ...]
    page_occupancies: tuple[float | None, ...]
    page_word_counts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PdfRasterizedPage:
    content: bytes
    etag: str
    pdf_sha256: str
    page: int
    profile: PdfInspectionProfile


class PdfInspectionSourceReader(Protocol):
    async def find_by_output_sha256(self, pdf_sha256: str) -> tuple[PdfRenderReference, ...]: ...

    async def find_by_render_id(self, render_id: UUID) -> PdfRenderReference | None: ...


class PdfInspectionBlobReader(Protocol):
    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...


class PdfInspectionCache(Protocol):
    def get_preview(
        self, pdf_sha256: str
    ) -> tuple[bytes, tuple[dict[str, Any], ...], bool] | None: ...

    def put_preview(self, pdf_sha256: str, content: bytes, provenance: dict[str, Any]) -> None: ...

    def get_derived(self, key: str) -> bytes | None: ...

    def put_derived(self, key: str, content: bytes) -> None: ...

    def delete_derived(self, key: str) -> None: ...


class PdfInspectionProcessor(Protocol):
    @property
    def build(self) -> str: ...

    async def inspect(self, content: bytes) -> PdfInspectionResult: ...

    async def page_size(self, content: bytes, page: int) -> tuple[float, float] | None: ...

    async def rasterize(
        self, content: bytes, page: int, profile: PdfInspectionProfile
    ) -> bytes: ...


class PdfInspectionService:
    """Resolve verified PDF bytes by their output SHA-256 and inspect projections."""

    def __init__(
        self,
        sources: PdfInspectionSourceReader,
        blob_reader: PdfInspectionBlobReader,
        processor: PdfInspectionProcessor,
        cache: PdfInspectionCache,
    ) -> None:
        self._sources = sources
        self._blob_reader = blob_reader
        self._processor = processor
        self._cache = cache

    async def cache_preview_pdf(
        self,
        content: bytes,
        *,
        edition_id: UUID,
        preview_input_hash: str,
    ) -> str:
        if len(content) > MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
            raise PdfInspectionError(PdfInspectionErrorCode.LIMIT_EXCEEDED)
        pdf_sha256 = hashlib.sha256(content).hexdigest()
        self._cache.put_preview(
            pdf_sha256,
            content,
            {
                "kind": "edition_preview",
                "edition_id": str(edition_id),
                "preview_input_hash": preview_input_hash,
            },
        )
        return pdf_sha256

    async def metadata(self, pdf_sha256: str) -> dict[str, Any]:
        source = await self._resolve(pdf_sha256)
        inspection = await self._inspection(source.content, pdf_sha256)
        mapping = await self._mapping(source, pdf_sha256)
        size_points = (
            [inspection.page_width_points, inspection.page_height_points]
            if inspection.page_width_points is not None
            and inspection.page_height_points is not None
            else None
        )
        diagnostics: list[dict[str, Any]] = [
            {
                "code": "pdf_integrity_verified",
                "class": PdfDiagnosticClass.DETERMINISTIC,
                "page": None,
                "measurement": {
                    "sha256": pdf_sha256,
                    "byte_size": len(source.content),
                },
                "target": None,
            }
        ]
        if not inspection.readable:
            diagnostics.append(
                {
                    "code": "pdf_unreadable",
                    "class": PdfDiagnosticClass.DETERMINISTIC,
                    "page": None,
                    "measurement": {"parser_warnings": list(inspection.parser_warnings)},
                    "target": None,
                }
            )
        if inspection.encrypted:
            diagnostics.append(
                {
                    "code": "pdf_encrypted",
                    "class": PdfDiagnosticClass.DETERMINISTIC,
                    "page": None,
                    "measurement": {"encrypted": True},
                    "target": None,
                }
            )
        if inspection.page_count > MAX_PDF_PAGES:
            diagnostics.append(
                {
                    "code": "page_count_limit_exceeded",
                    "class": PdfDiagnosticClass.DETERMINISTIC,
                    "page": None,
                    "measurement": {
                        "page_count": inspection.page_count,
                        "maximum": MAX_PDF_PAGES,
                    },
                    "target": None,
                }
            )
        if size_points is not None and self._extreme_dimensions(
            float(size_points[0]), float(size_points[1])
        ):
            diagnostics.append(
                {
                    "code": "page_dimensions_limit_exceeded",
                    "class": PdfDiagnosticClass.DETERMINISTIC,
                    "page": 1,
                    "measurement": {
                        "width_points": size_points[0],
                        "height_points": size_points[1],
                        "maximum_edge_points": 20_000,
                        "maximum_aspect_ratio": 20,
                    },
                    "target": None,
                }
            )
        if inspection.non_embedded_fonts:
            diagnostics.append(
                {
                    "code": "font_not_embedded",
                    "class": PdfDiagnosticClass.DETERMINISTIC,
                    "page": None,
                    "measurement": {"fonts": list(inspection.non_embedded_fonts)},
                    "target": None,
                }
            )
        if inspection.readable and not inspection.encrypted:
            for page, ratio in enumerate(inspection.page_occupancies, start=1):
                words = inspection.page_word_counts[page - 1]
                if words == 0:
                    diagnostics.append(
                        {
                            "code": "page_blank",
                            "class": PdfDiagnosticClass.HEURISTIC,
                            "page": page,
                            "measurement": {
                                "text_word_count": 0,
                                "threshold": 0,
                                "basis": (
                                    "pdftotext bbox word count; image-only pages may be "
                                    "false positives"
                                ),
                            },
                            "target": None,
                        }
                    )
                elif ratio is not None and ratio < LOW_OCCUPANCY_THRESHOLD:
                    diagnostics.append(
                        {
                            "code": "page_low_occupancy",
                            "class": PdfDiagnosticClass.HEURISTIC,
                            "page": page,
                            "measurement": {
                                "text_box_area_ratio": round(ratio, 6),
                                "threshold": LOW_OCCUPANCY_THRESHOLD,
                                "basis": (
                                    "sum of pdftotext word-box areas / page area; "
                                    "overlapping boxes may be counted twice"
                                ),
                            },
                            "target": None,
                        }
                    )

        diagnostics.extend(self._localized_block_diagnostics(mapping))

        dimensions_extreme = size_points is not None and self._extreme_dimensions(
            float(size_points[0]), float(size_points[1])
        )
        readable = inspection.readable and not inspection.encrypted
        if inspection.page_count > MAX_PDF_PAGES or dimensions_extreme:
            availability = PdfInspectionAvailability.LIMIT_EXCEEDED
        elif readable:
            availability = PdfInspectionAvailability.READY
        else:
            availability = PdfInspectionAvailability.CORRUPT

        render_metadata = []
        source_metadata = []
        for provenance in source.provenances:
            source_item = dict(provenance.source)
            if provenance.render is not None:
                render = provenance.render
                source_item["render_id"] = str(render.id)
                render_metadata.append(
                    {
                        "id": str(render.id),
                        "status": render.status,
                        "input_hash": render.input_hash,
                        "renderer_version": render.renderer_version,
                        "template_sha256": render.template_sha256,
                        "compiler_version": render.compiler_version,
                        "font_bundle_version": render.font_bundle_version,
                    }
                )
            source_metadata.append(source_item)

        return {
            "schema_version": "pdf-inspection-v1",
            "pdf": {
                "sha256": pdf_sha256,
                "byte_size": len(source.content),
                "page_count": inspection.page_count,
                "page_size_points": size_points,
                "encrypted": inspection.encrypted,
                "parser_warnings": list(inspection.parser_warnings),
            },
            "availability": availability,
            "render": render_metadata,
            "source": source_metadata,
            "raster": {
                "status": (
                    PdfRasterStatus.AVAILABLE
                    if availability is PdfInspectionAvailability.READY
                    else PdfRasterStatus.UNAVAILABLE
                ),
                "rasterizer": "poppler",
                "rasterizer_version": self._processor.build,
                "profiles": {
                    "preview": {"dpi": PREVIEW_DPI, "max_edge_px": PREVIEW_MAX_EDGE_PX},
                    "thumbnail": {"max_edge_px": THUMBNAIL_MAX_EDGE_PX},
                },
                "page_png_url_template": (
                    f"/api/pdf-inspections/{pdf_sha256}/pages/{{page}}.png?profile=preview"
                ),
                "thumbnail_url_template": (
                    f"/api/pdf-inspections/{pdf_sha256}/pages/{{page}}/thumbnail.png"
                ),
            },
            "diagnostics": {
                "status": "ready" if inspection.readable else "unavailable",
                "engine_version": DIAGNOSTICS_VERSION,
                "counts": {
                    "deterministic": sum(
                        item["class"] is PdfDiagnosticClass.DETERMINISTIC for item in diagnostics
                    ),
                    "heuristic": sum(
                        item["class"] is PdfDiagnosticClass.HEURISTIC for item in diagnostics
                    ),
                },
                "items": diagnostics,
            },
            "mapping": mapping,
        }

    async def _mapping(
        self,
        source: PdfInspectionSource,
        requested_pdf_sha256: str,
    ) -> dict[str, Any]:
        sidecars: list[dict[str, Any]] = []
        for provenance in source.provenances:
            render = provenance.render
            if render is None and provenance.source.get("kind") == "edition_preview":
                renderer_version = EDITION_RENDERER_VERSION
                render_id = str(provenance.source.get("preview_input_hash", ""))
            elif render is not None:
                renderer_version = render.renderer_version
                render_id = str(render.id)
            else:
                continue
            if not render_id:
                continue
            raw = self._cache.get_derived(
                mapping_cache_key(requested_pdf_sha256, renderer_version, render_id)
            )
            if raw is None:
                continue
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("pdf_sha256") != requested_pdf_sha256
                or payload.get("renderer_version") != renderer_version
                or payload.get("render_id") != render_id
            ):
                return {
                    "status": PdfMappingStatus.UNVERIFIED,
                    "reason": "mapping_sidecar_provenance_mismatch",
                    "items": [],
                    "unmapped": [],
                }
            sidecars.append(payload)

        if not sidecars:
            return {
                "status": PdfMappingStatus.NOT_AVAILABLE,
                "reason": "location_sidecar_unavailable",
                "items": [],
                "unmapped": [],
            }

        projections: list[dict[str, Any]] = []
        for sidecar in sidecars:
            descriptors = sidecar.get("targets")
            unmapped = sidecar.get("unmapped")
            query_result = sidecar.get("query")
            if not isinstance(descriptors, list):
                continue
            pages: set[int] = set()
            if isinstance(query_result, list):
                for row in query_result:
                    if not isinstance(row, dict) or not isinstance(row.get("value"), dict):
                        continue
                    value = row["value"]
                    if isinstance(value.get("page"), int) and value["page"] > 0:
                        pages.add(value["page"])
            page_sizes: dict[int, tuple[float, float]] = {}
            for page in sorted(pages):
                try:
                    size = await self._processor.page_size(source.content, page)
                except Exception:
                    size = None
                if size is not None:
                    page_sizes[page] = size
            projections.append(
                build_mapping_projection(
                    pdf_sha256=str(sidecar.get("pdf_sha256", "")),
                    requested_pdf_sha256=requested_pdf_sha256,
                    descriptors=descriptors,
                    unmapped=unmapped if isinstance(unmapped, list) else [],
                    query_result=query_result,
                    page_sizes=page_sizes,
                )
            )

        if not projections:
            return {
                "status": PdfMappingStatus.NOT_AVAILABLE,
                "reason": "location_sidecar_invalid",
                "items": [],
                "unmapped": [],
            }
        items = [item for projection in projections for item in projection["items"]]
        unmapped_items = [item for projection in projections for item in projection["unmapped"]]
        if not items:
            return {
                "status": PdfMappingStatus.NOT_AVAILABLE,
                "reason": "layout_location_unavailable",
                "items": [],
                "unmapped": unmapped_items,
            }
        return {
            "status": PdfMappingStatus.PARTIAL,
            "reason": None,
            "items": items,
            "unmapped": unmapped_items,
        }

    @staticmethod
    def _localized_block_diagnostics(mapping: dict[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for item in mapping.get("items", []):
            target = item.get("target", {})
            block_type = target.get("block_type")
            if block_type not in {"diagram", "chart", "source_figure"}:
                continue
            for location in item.get("locations", []):
                bbox = location.get("bbox_points", {})
                width = bbox.get("width")
                height = bbox.get("height")
                if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
                    continue
                if (
                    width >= BLOCK_TOO_SMALL_MIN_WIDTH_POINTS
                    and height >= BLOCK_TOO_SMALL_MIN_HEIGHT_POINTS
                ):
                    continue
                findings.append(
                    {
                        "code": "block_too_small",
                        "class": PdfDiagnosticClass.DETERMINISTIC,
                        "page": location.get("page"),
                        "measurement": {
                            "width_points": width,
                            "height_points": height,
                            "minimum_width_points": BLOCK_TOO_SMALL_MIN_WIDTH_POINTS,
                            "minimum_height_points": BLOCK_TOO_SMALL_MIN_HEIGHT_POINTS,
                            "basis": "Typst measured layout slot for a canonical figure block",
                        },
                        "target": {
                            "target_id": item.get("target_id"),
                            **dict(target),
                        },
                    }
                )
        return findings

    async def page_png(
        self,
        pdf_sha256: str,
        page: int,
        profile: PdfInspectionProfile,
    ) -> PdfRasterizedPage:
        try:
            async with asyncio.timeout(15):
                async with _PAGE_REQUEST_SEMAPHORE:
                    return await self._page_png(pdf_sha256, page, profile)
        except TimeoutError as exc:
            raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT) from exc

    async def _page_png(
        self,
        pdf_sha256: str,
        page: int,
        profile: PdfInspectionProfile,
    ) -> PdfRasterizedPage:
        source = await self._resolve(pdf_sha256)
        inspection = await self._inspection(source.content, pdf_sha256)
        if not inspection.readable or inspection.encrypted:
            raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT)
        if inspection.page_count > MAX_PDF_PAGES:
            raise PdfInspectionError(PdfInspectionErrorCode.LIMIT_EXCEEDED)
        if page < 1 or page > inspection.page_count:
            raise PdfInspectionError(PdfInspectionErrorCode.PAGE_OUT_OF_BOUNDS)
        try:
            page_size = await self._processor.page_size(source.content, page)
        except Exception as exc:
            raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT) from exc
        if page_size is None or self._extreme_dimensions(*page_size):
            raise PdfInspectionError(PdfInspectionErrorCode.LIMIT_EXCEEDED)

        cache_key = self._raster_cache_key(pdf_sha256, profile, page)
        png = self._cache.get_derived(cache_key)
        if png is not None and not self._valid_png(png, profile):
            self._cache.delete_derived(cache_key)
            png = None
        if png is None:
            try:
                png = await self._processor.rasterize(source.content, page, profile)
            except Exception as exc:
                raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT) from exc
            if not self._valid_png(png, profile):
                raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT)
            self._cache.put_derived(cache_key, png)
        return PdfRasterizedPage(
            content=png,
            etag=f'"{hashlib.sha256(png).hexdigest()}"',
            pdf_sha256=pdf_sha256,
            page=page,
            profile=profile,
        )

    async def _resolve(self, pdf_sha256: str) -> PdfInspectionSource:
        if _SHA256.fullmatch(pdf_sha256) is None:
            raise PdfInspectionError(PdfInspectionErrorCode.HASH_INVALID)
        references = await self._sources.find_by_output_sha256(pdf_sha256)
        provenances: list[PdfResolvedProvenance] = []
        content: bytes | None = None
        for reference in references:
            if reference.status != "succeeded":
                continue
            if (
                reference.output_blob_id is None
                or reference.output_byte_size is None
                or reference.output_sha256 is None
            ):
                continue
            if reference.output_byte_size > MAX_PDF_BYTES:
                raise PdfInspectionError(PdfInspectionErrorCode.LIMIT_EXCEEDED)
            try:
                observed = await self._blob_reader.read_bytes(
                    reference.output_blob_id, max_bytes=MAX_PDF_BYTES
                )
            except Exception as exc:
                raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT) from exc
            if len(observed) > MAX_PDF_BYTES:
                raise PdfInspectionError(PdfInspectionErrorCode.LIMIT_EXCEEDED)
            if (
                len(observed) != reference.output_byte_size
                or hashlib.sha256(observed).hexdigest() != reference.output_sha256
                or reference.output_sha256 != pdf_sha256
                or not observed.startswith(b"%PDF-")
            ):
                raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT)
            if content is None:
                content = observed
            elif content != observed:
                raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT)
            provenances.append(PdfResolvedProvenance(reference, reference.source))

        preview = self._cache.get_preview(pdf_sha256)
        if preview is not None:
            preview_content, preview_sources, expired = preview
            if expired:
                if content is None:
                    raise PdfInspectionError(PdfInspectionErrorCode.PREVIEW_CACHE_EXPIRED)
            else:
                if (
                    len(preview_content) > MAX_PDF_BYTES
                    or hashlib.sha256(preview_content).hexdigest() != pdf_sha256
                    or not preview_content.startswith(b"%PDF-")
                ):
                    raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT)
                if content is not None and content != preview_content:
                    raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT)
                content = preview_content
                provenances.extend(PdfResolvedProvenance(None, item) for item in preview_sources)
        if content is None:
            raise PdfInspectionError(PdfInspectionErrorCode.ABSENT)
        if len(content) > MAX_PDF_BYTES:
            raise PdfInspectionError(PdfInspectionErrorCode.LIMIT_EXCEEDED)
        return PdfInspectionSource(content, tuple(provenances), preview is not None)

    async def _inspection(self, content: bytes, pdf_sha256: str) -> PdfInspectionResult:
        key = hashlib.sha256(
            f"{pdf_sha256}:{self._processor.build}:{INSPECTION_POLICY_VERSION}:"
            f"{DIAGNOSTICS_VERSION}".encode()
        ).hexdigest()
        cached = self._cache.get_derived(f"inspection:{key}")
        if cached is not None:
            try:
                value = json.loads(cached)
                return PdfInspectionResult(
                    readable=bool(value["readable"]),
                    page_count=int(value["page_count"]),
                    page_width_points=(
                        float(value["page_width_points"])
                        if value["page_width_points"] is not None
                        else None
                    ),
                    page_height_points=(
                        float(value["page_height_points"])
                        if value["page_height_points"] is not None
                        else None
                    ),
                    encrypted=bool(value["encrypted"]),
                    parser_warnings=tuple(value["parser_warnings"]),
                    non_embedded_fonts=tuple(value["non_embedded_fonts"]),
                    page_occupancies=tuple(value["page_occupancies"]),
                    page_word_counts=tuple(value["page_word_counts"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._cache.delete_derived(f"inspection:{key}")
        try:
            async with asyncio.timeout(30):
                result = await self._processor.inspect(content)
        except Exception as exc:
            raise PdfInspectionError(PdfInspectionErrorCode.OUTPUT_CORRUPT) from exc
        self._cache.put_derived(
            f"inspection:{key}",
            json.dumps(asdict(result), separators=(",", ":")).encode(),
        )
        return result

    def _raster_cache_key(self, pdf_sha256: str, profile: PdfInspectionProfile, page: int) -> str:
        identity = ":".join(
            (
                pdf_sha256,
                self._processor.build,
                profile.value,
                str(page),
                INSPECTION_POLICY_VERSION,
            )
        )
        return hashlib.sha256(identity.encode()).hexdigest()

    @staticmethod
    def _extreme_dimensions(width: float, height: float) -> bool:
        if width <= 0 or height <= 0 or width > 20_000 or height > 20_000:
            return True
        return max(width, height) / min(width, height) > 20

    @staticmethod
    def _valid_png(content: bytes, profile: PdfInspectionProfile) -> bool:
        import zlib

        if len(content) < 45 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        max_edge = (
            PREVIEW_MAX_EDGE_PX
            if profile is PdfInspectionProfile.PREVIEW
            else THUMBNAIL_MAX_EDGE_PX
        )
        if len(content) > 25 * 1024 * 1024:
            return False
        offset = 8
        width = height = 0
        saw_ihdr = saw_idat = saw_iend = False
        while offset + 12 <= len(content):
            chunk_length = int.from_bytes(content[offset : offset + 4], "big")
            chunk_type = content[offset + 4 : offset + 8]
            chunk_end = offset + 12 + chunk_length
            if chunk_end > len(content):
                return False
            chunk_data = content[offset + 8 : offset + 8 + chunk_length]
            expected_crc = int.from_bytes(content[offset + 8 + chunk_length : chunk_end], "big")
            if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
                return False
            if not saw_ihdr:
                if chunk_type != b"IHDR" or chunk_length != 13:
                    return False
                width = int.from_bytes(chunk_data[:4], "big")
                height = int.from_bytes(chunk_data[4:8], "big")
                saw_ihdr = True
            elif chunk_type == b"IHDR":
                return False
            if chunk_type == b"IDAT":
                saw_idat = True
            if chunk_type == b"IEND":
                if chunk_length != 0 or chunk_end != len(content):
                    return False
                saw_iend = True
                break
            offset = chunk_end
        return (
            saw_ihdr
            and saw_idat
            and saw_iend
            and width > 0
            and height > 0
            and max(width, height) <= max_edge
        )

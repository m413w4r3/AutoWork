from __future__ import annotations

import asyncio
import binascii
import hashlib
import io
import json
import os
import struct
import zlib
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.pdf_inspection import router
from cti_app.application.pdf_inspection import (
    MAX_PDF_PAGES,
    PdfInspectionError,
    PdfInspectionResult,
    PdfInspectionService,
    PdfRenderReference,
)
from cti_app.application.pdf_mapping import mapping_cache_key, target_descriptor
from cti_app.domain.pdf_inspection import PdfInspectionErrorCode, PdfInspectionProfile
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache, PopplerSubprocess

PDF = b"%PDF-1.7\nfixture-content\n"
PDF_SHA256 = hashlib.sha256(PDF).hexdigest()
RENDER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _png(width: int = 800, height: int = 1100) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = binascii.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    row = b"\x00" + b"\xff\xff\xff" * width
    compressed = zlib.compress(row * height)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def _reference(
    content: bytes = PDF, *, source: dict[str, object] | None = None
) -> PdfRenderReference:
    digest = hashlib.sha256(content).hexdigest()
    return PdfRenderReference(
        id=RENDER_ID,
        kind="publication_render",
        status="succeeded",
        input_hash="b" * 64,
        renderer_version="typst-publication-test",
        template_sha256="c" * 64,
        compiler_version="0.15.1",
        font_bundle_version="fonts-v1",
        output_blob_id=UUID("00000000-0000-0000-0000-000000000002"),
        output_sha256=digest,
        output_byte_size=len(content),
        source=source
        or {
            "kind": "subject_publication",
            "subject_id": "00000000-0000-0000-0000-000000000010",
            "artifact_id": "00000000-0000-0000-0000-000000000011",
            "artifact_version": 3,
            "artifact_input_hash": "d" * 64,
        },
    )


class _Sources:
    def __init__(self, references: tuple[PdfRenderReference, ...] | None = None) -> None:
        self.references = references if references is not None else (_reference(),)

    async def find_by_output_sha256(self, pdf_sha256: str) -> tuple[PdfRenderReference, ...]:
        return tuple(item for item in self.references if item.output_sha256 == pdf_sha256)


class _BlobReader:
    def __init__(self, content: bytes = PDF) -> None:
        self.content = content

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        del blob_id
        if len(self.content) > max_bytes:
            raise ValueError("too large")
        return self.content


class _Processor:
    build = "poppler-24.02.0"

    def __init__(self, result: PdfInspectionResult | None = None) -> None:
        self.result = result or PdfInspectionResult(
            readable=True,
            page_count=2,
            page_width_points=595.276,
            page_height_points=841.89,
            encrypted=False,
            parser_warnings=("Syntax Error: Suspects object is wrong type (boolean)",),
            non_embedded_fonts=("MissingFont",),
            page_occupancies=(0.7, None),
            page_word_counts=(20, 0),
        )
        self.page_dimensions: tuple[float, float] | None = (595.276, 841.89)
        self.raster_content = _png()
        self.raster_calls = 0
        self.fail_inspection: Exception | None = None
        self.fail_raster: Exception | None = None

    async def inspect(self, content: bytes) -> PdfInspectionResult:
        del content
        if self.fail_inspection is not None:
            raise self.fail_inspection
        return self.result

    async def page_size(self, content: bytes, page: int) -> tuple[float, float] | None:
        del content, page
        return self.page_dimensions

    async def rasterize(self, content: bytes, page: int, profile: PdfInspectionProfile) -> bytes:
        del content, page
        self.raster_calls += 1
        if self.fail_raster is not None:
            raise self.fail_raster
        return _png(400, 480) if profile is PdfInspectionProfile.THUMBNAIL else self.raster_content


def _service(
    tmp_path: Path,
    *,
    content: bytes = PDF,
    processor: _Processor | None = None,
    references: tuple[PdfRenderReference, ...] | None = None,
    cache: PdfInspectionCache | None = None,
) -> PdfInspectionService:
    return PdfInspectionService(
        _Sources(references),
        _BlobReader(content),
        processor or _Processor(),
        cache or PdfInspectionCache(tmp_path / "cache"),
    )


@pytest.mark.asyncio
async def test_metadata_exposes_integrity_parser_warnings_and_heuristic_measurements(
    tmp_path: Path,
) -> None:
    body = await _service(tmp_path).metadata(PDF_SHA256)

    assert body["schema_version"] == "pdf-inspection-v1"
    assert body["pdf"]["sha256"] == PDF_SHA256
    assert body["pdf"]["parser_warnings"] == [
        "Syntax Error: Suspects object is wrong type (boolean)"
    ]
    assert body["mapping"] == {
        "status": "not_available",
        "reason": "location_sidecar_unavailable",
        "items": [],
        "unmapped": [],
    }
    assert body["source"][0]["kind"] == "subject_publication"
    assert body["source"][0]["render_id"] == str(RENDER_ID)
    assert body["diagnostics"]["counts"] == {"deterministic": 2, "heuristic": 1}
    blank = next(item for item in body["diagnostics"]["items"] if item["code"] == "page_blank")
    assert blank["class"] == "heuristic"
    assert blank["page"] == 2
    assert blank["measurement"]["text_word_count"] == 0
    assert all(item["target"] is None for item in body["diagnostics"]["items"])


@pytest.mark.asyncio
async def test_page_range_profiles_headers_and_raster_cache_are_deterministic(
    tmp_path: Path,
) -> None:
    processor = _Processor()
    service = _service(tmp_path, processor=processor)
    first = await service.page_png(PDF_SHA256, 1, PdfInspectionProfile.PREVIEW)
    repeated = await service.page_png(PDF_SHA256, 1, PdfInspectionProfile.PREVIEW)
    last = await service.page_png(PDF_SHA256, 2, PdfInspectionProfile.THUMBNAIL)

    assert first.content == repeated.content
    assert first.etag == repeated.etag == f'"{hashlib.sha256(_png()).hexdigest()}"'
    assert first.page == 1 and first.profile is PdfInspectionProfile.PREVIEW
    assert last.page == 2 and last.profile is PdfInspectionProfile.THUMBNAIL
    assert processor.raster_calls == 2
    with pytest.raises(PdfInspectionError) as error:
        await service.page_png(PDF_SHA256, 3, PdfInspectionProfile.PREVIEW)
    assert error.value.code is PdfInspectionErrorCode.PAGE_OUT_OF_BOUNDS


@pytest.mark.asyncio
async def test_raster_cache_key_changes_with_pdf_bytes_and_poppler_build(tmp_path: Path) -> None:
    cache = PdfInspectionCache(tmp_path / "cache")
    first_processor = _Processor()
    first_service = _service(tmp_path, processor=first_processor, cache=cache)
    await first_service.page_png(PDF_SHA256, 1, PdfInspectionProfile.PREVIEW)

    new_build = _Processor()
    new_build.build = "poppler-25.0.0"
    new_build_service = _service(tmp_path, processor=new_build, cache=cache)
    await new_build_service.page_png(PDF_SHA256, 1, PdfInspectionProfile.PREVIEW)

    other_pdf = b"%PDF-1.7\nother-pdf-bytes\n"
    other_sha256 = hashlib.sha256(other_pdf).hexdigest()
    other_processor = _Processor()
    other_service = PdfInspectionService(
        _Sources((_reference(other_pdf),)),
        _BlobReader(other_pdf),
        other_processor,
        cache,
    )
    await other_service.page_png(other_sha256, 1, PdfInspectionProfile.PREVIEW)

    assert first_processor.raster_calls == 1
    assert new_build.raster_calls == 1
    assert other_processor.raster_calls == 1


@pytest.mark.asyncio
async def test_local_typst_fixture_is_parsed_and_rasterizes_reproducibly(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "pdf_inspection" / "typst-generated.pdf"
    content = fixture.read_bytes()
    pdf_sha256 = hashlib.sha256(content).hexdigest()
    processor = PopplerSubprocess(temp_root=tmp_path / "poppler")

    inspection = await processor.inspect(content)
    assert inspection.readable is True
    assert inspection.page_count == 2
    assert inspection.encrypted is False
    assert inspection.page_width_points == pytest.approx(595.276)
    assert any("Suspects object is wrong type" in warning for warning in inspection.parser_warnings)
    assert inspection.page_word_counts[0] > 0
    assert inspection.page_word_counts[1] == 0
    assert PopplerSubprocess._non_embedded_fonts(
        "name type encoding emb sub uni object ID\n"
        "---- ---- -------- --- --- --- ---------\n"
        "MissingFont Type 1 WinAnsi no no no 4 0\n"
    ) == ("MissingFont",)

    first = await processor.rasterize(content, 1, PdfInspectionProfile.PREVIEW)
    repeated = await processor.rasterize(content, 1, PdfInspectionProfile.PREVIEW)
    last = await processor.rasterize(content, 2, PdfInspectionProfile.THUMBNAIL)
    assert hashlib.sha256(first).digest() == hashlib.sha256(repeated).digest()
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert last.startswith(b"\x89PNG\r\n\x1a\n")

    service = PdfInspectionService(
        _Sources((_reference(content),)),
        _BlobReader(content),
        processor,
        PdfInspectionCache(tmp_path / "service-cache"),
    )
    metadata = await service.metadata(pdf_sha256)
    assert metadata["pdf"]["sha256"] == pdf_sha256
    assert metadata["pdf"]["byte_size"] == len(content)
    assert metadata["pdf"]["page_count"] == 2
    assert metadata["mapping"]["status"] == "not_available"
    assert metadata["mapping"]["reason"] == "location_sidecar_unavailable"


@pytest.mark.asyncio
async def test_poppler_reports_truncated_encrypted_and_extreme_typst_pdfs(
    tmp_path: Path,
) -> None:
    from pypdf import PdfReader, PdfWriter

    fixture = Path(__file__).parent / "fixtures" / "pdf_inspection" / "typst-generated.pdf"
    content = fixture.read_bytes()
    processor = PopplerSubprocess(temp_root=tmp_path / "poppler")

    truncated = await processor.inspect(content[:-100])
    assert truncated.readable is False
    assert truncated.parser_warnings

    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    writer.encrypt(user_password="", owner_password="owner-secret")
    encrypted_stream = io.BytesIO()
    writer.write(encrypted_stream)
    encrypted = await processor.inspect(encrypted_stream.getvalue())
    assert encrypted.encrypted is True

    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    page = reader.pages[0]
    page.mediabox.upper_right = (25_000, 842)
    writer.add_page(page)
    extreme_stream = io.BytesIO()
    writer.write(extreme_stream)
    extreme_content = extreme_stream.getvalue()
    extreme_sha256 = hashlib.sha256(extreme_content).hexdigest()
    extreme_service = PdfInspectionService(
        _Sources((_reference(extreme_content),)),
        _BlobReader(extreme_content),
        processor,
        PdfInspectionCache(tmp_path / "extreme-cache"),
    )
    metadata = await extreme_service.metadata(extreme_sha256)
    assert metadata["availability"] == "limit_exceeded"
    with pytest.raises(PdfInspectionError) as error:
        await extreme_service.page_png(extreme_sha256, 1, PdfInspectionProfile.PREVIEW)
    assert error.value.code is PdfInspectionErrorCode.LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_hash_resolution_refuses_unknown_and_corrupt_registered_output(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, references=())
    with pytest.raises(PdfInspectionError) as absent:
        await service.metadata("e" * 64)
    assert absent.value.code is PdfInspectionErrorCode.ABSENT

    mismatched = _service(tmp_path / "mismatch", content=b"%PDF-1.7 changed")
    with pytest.raises(PdfInspectionError) as corrupt:
        await mismatched.metadata(PDF_SHA256)
    assert corrupt.value.code is PdfInspectionErrorCode.OUTPUT_CORRUPT

    with pytest.raises(PdfInspectionError) as invalid_hash:
        await service.metadata("../not-a-hash")
    assert invalid_hash.value.code is PdfInspectionErrorCode.HASH_INVALID


@pytest.mark.asyncio
async def test_one_pdf_hash_returns_every_persisted_provenance(tmp_path: Path) -> None:
    publication = _reference()
    edition = replace(
        publication,
        id=UUID("00000000-0000-0000-0000-000000000003"),
        kind="edition_render",
        source={
            "kind": "edition_release",
            "edition_id": "00000000-0000-0000-0000-000000000020",
            "release_id": "00000000-0000-0000-0000-000000000021",
            "manifest_id": "00000000-0000-0000-0000-000000000022",
            "manifest_sha256": "a" * 64,
            "edition_document_sha256": "b" * 64,
            "artifacts": [],
        },
    )
    body = await _service(tmp_path, references=(publication, edition)).metadata(PDF_SHA256)

    assert len(body["render"]) == 2
    assert [item["kind"] for item in body["source"]] == [
        "subject_publication",
        "edition_release",
    ]
    assert {item["render_id"] for item in body["source"]} == {
        str(publication.id),
        str(edition.id),
    }


@pytest.mark.asyncio
async def test_verified_typst_sidecar_maps_canonical_diagram_and_localizes_diagnostic(
    tmp_path: Path,
) -> None:
    cache = PdfInspectionCache(tmp_path / "cache")
    reference = _reference()
    descriptor = target_descriptor(
        subject_id="00000000-0000-0000-0000-000000000010",
        artifact_id="00000000-0000-0000-0000-000000000011",
        artifact_version=3,
        artifact_input_hash="d" * 64,
        block_type="diagram",
        block_key="overview",
    )
    sidecar = {
        "pdf_sha256": PDF_SHA256,
        "renderer_version": reference.renderer_version,
        "render_id": str(reference.id),
        "targets": [descriptor],
        "unmapped": [{"block_type": "paragraph", "reason": "no_stable_identity", "count": 1}],
        "query": [
            {
                "func": "metadata",
                "value": {
                    "target_id": descriptor["target_id"],
                    "boundary": "location_start",
                    "page": 1,
                    "x": "62.36pt",
                    "y": "180pt",
                    "width": "470.55pt",
                    "height": "50pt",
                },
            },
            {
                "func": "metadata",
                "value": {
                    "target_id": descriptor["target_id"],
                    "boundary": "location_end",
                    "page": 1,
                    "x": "62.36pt",
                    "y": "230pt",
                },
            },
        ],
    }
    cache.put_derived(
        mapping_cache_key(PDF_SHA256, reference.renderer_version, str(reference.id)),
        json.dumps(sidecar).encode(),
    )
    service = _service(tmp_path, cache=cache, references=(reference,))

    body = await service.metadata(PDF_SHA256)

    mapped = body["mapping"]["items"][0]
    assert body["mapping"]["status"] == "partial"
    assert mapped["target_id"] == descriptor["target_id"]
    assert mapped["target"]["block_key"] == "overview"
    assert mapped["target"]["revision_channel"] == {
        "family": "editorial_enrichment",
        "operations": ["rewrite_diagram", "delete_diagram"],
    }
    assert mapped["mapping_quality"] == "canonical_block_key"
    assert mapped["locations"] == [
        {"page": 1, "bbox_points": {"x": 62.36, "y": 180.0, "width": 470.55, "height": 50.0}}
    ]
    assert body["mapping"]["unmapped"] == [
        {"block_type": "paragraph", "reason": "no_stable_identity", "count": 1}
    ]
    finding = next(
        item for item in body["diagnostics"]["items"] if item["code"] == "block_too_small"
    )
    assert finding["class"] == "deterministic"
    assert finding["target"]["target_id"] == descriptor["target_id"]

    sidecar["query"][0]["value"].update(page=2, y="100pt")
    sidecar["query"][1]["value"].update(page=2, y="160pt")
    cache.put_derived(
        mapping_cache_key(PDF_SHA256, reference.renderer_version, str(reference.id)),
        json.dumps(sidecar).encode(),
    )
    reflowed = await service.metadata(PDF_SHA256)
    moved = reflowed["mapping"]["items"][0]
    assert moved["target_id"] == mapped["target_id"]
    assert moved["locations"][0]["page"] == 2
    assert moved["locations"][0]["bbox_points"]["y"] == 100.0


@pytest.mark.asyncio
async def test_unreadable_encrypted_oversized_and_extreme_dimensions_have_stable_states(
    tmp_path: Path,
) -> None:
    unreadable = _Processor(
        PdfInspectionResult(False, 0, None, None, False, ("Syntax Error",), (), (), ())
    )
    body = await _service(tmp_path / "truncated", processor=unreadable).metadata(PDF_SHA256)
    assert body["availability"] == "corrupt"
    assert any(item["code"] == "pdf_unreadable" for item in body["diagnostics"]["items"])
    with pytest.raises(PdfInspectionError) as invalid:
        await _service(tmp_path / "invalid", processor=unreadable).page_png(
            PDF_SHA256, 1, PdfInspectionProfile.PREVIEW
        )
    assert invalid.value.code is PdfInspectionErrorCode.OUTPUT_CORRUPT

    encrypted = _Processor(PdfInspectionResult(True, 1, 595.0, 842.0, True, (), (), (), ()))
    encrypted_body = await _service(tmp_path / "encrypted", processor=encrypted).metadata(
        PDF_SHA256
    )
    assert encrypted_body["availability"] == "corrupt"
    assert any(item["code"] == "pdf_encrypted" for item in encrypted_body["diagnostics"]["items"])

    too_many = _Processor(
        PdfInspectionResult(True, MAX_PDF_PAGES + 1, 595.0, 842.0, False, (), (), (), ())
    )
    limited = await _service(tmp_path / "pages", processor=too_many).metadata(PDF_SHA256)
    assert limited["availability"] == "limit_exceeded"
    with pytest.raises(PdfInspectionError) as page_limit:
        await _service(tmp_path / "pages2", processor=too_many).page_png(
            PDF_SHA256, 1, PdfInspectionProfile.PREVIEW
        )
    assert page_limit.value.code is PdfInspectionErrorCode.LIMIT_EXCEEDED

    extreme = _Processor()
    extreme.page_dimensions = (25_000.0, 842.0)
    with pytest.raises(PdfInspectionError) as dimension_limit:
        await _service(tmp_path / "dimensions", processor=extreme).page_png(
            PDF_SHA256, 1, PdfInspectionProfile.PREVIEW
        )
    assert dimension_limit.value.code is PdfInspectionErrorCode.LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_processor_timeout_and_invalid_output_are_translated_to_corrupt(
    tmp_path: Path,
) -> None:
    timeout = _Processor()
    timeout.fail_inspection = TimeoutError("simulated timeout")
    with pytest.raises(PdfInspectionError) as timeout_error:
        await _service(tmp_path / "timeout", processor=timeout).metadata(PDF_SHA256)
    assert timeout_error.value.code is PdfInspectionErrorCode.OUTPUT_CORRUPT

    invalid = _Processor()
    invalid.raster_content = b"not a png"
    with pytest.raises(PdfInspectionError) as invalid_error:
        await _service(tmp_path / "invalid-output", processor=invalid).page_png(
            PDF_SHA256, 1, PdfInspectionProfile.PREVIEW
        )
    assert invalid_error.value.code is PdfInspectionErrorCode.OUTPUT_CORRUPT


@pytest.mark.asyncio
async def test_preview_pdf_cache_expires_and_never_becomes_a_render_provenance(
    tmp_path: Path,
) -> None:
    cache = PdfInspectionCache(tmp_path / "cache", preview_ttl_seconds=0)
    service = _service(tmp_path, references=(), cache=cache)
    digest = await service.cache_preview_pdf(
        PDF,
        edition_id=UUID("00000000-0000-0000-0000-000000000020"),
        preview_input_hash="f" * 64,
    )
    assert digest == PDF_SHA256
    with pytest.raises(PdfInspectionError) as expired:
        await service.metadata(digest)
    assert expired.value.code is PdfInspectionErrorCode.PREVIEW_CACHE_EXPIRED


def test_cache_ttl_and_lru_limits_are_enforced(tmp_path: Path) -> None:
    cache = PdfInspectionCache(tmp_path / "lru", derived_max_bytes=5)
    cache.put_derived("old", b"1234")
    os.utime(cache._derived_path("old"), (1, 1))
    cache.put_derived("new", b"5678")
    assert cache.get_derived("old") is None
    assert cache.get_derived("new") == b"5678"

    expiring = PdfInspectionCache(tmp_path / "ttl", derived_ttl_seconds=0)
    expiring.put_derived("expires", b"content")
    assert expiring.get_derived("expires") is None

    previews = PdfInspectionCache(tmp_path / "previews", preview_max_bytes=5)
    previews.put_preview("a" * 64, b"1234", {"kind": "edition_preview"})
    previews.put_preview("b" * 64, b"5678", {"kind": "edition_preview"})
    assert previews.get_preview("a" * 64) == (b"", (), True)
    assert previews.get_preview("b" * 64) == (b"5678", ({"kind": "edition_preview"},), False)


@pytest.mark.asyncio
async def test_api_routes_return_typed_errors_and_png_headers(tmp_path: Path) -> None:
    application = FastAPI()
    application.include_router(router)
    application.state.pdf_inspection_service = _service(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        metadata = await client.get(f"/api/pdf-inspections/{PDF_SHA256}")
        page = await client.get(f"/api/pdf-inspections/{PDF_SHA256}/pages/1.png?profile=preview")
        thumbnail = await client.get(f"/api/pdf-inspections/{PDF_SHA256}/pages/2/thumbnail.png")
        absent = await client.get(f"/api/pdf-inspections/{'e' * 64}")
        invalid_hash = await client.get("/api/pdf-inspections/not-a-hash")
        out_of_bounds = await client.get(
            f"/api/pdf-inspections/{PDF_SHA256}/pages/3.png?profile=preview"
        )
        path_attempt = await client.get("/api/pdf-inspections/%2Fetc%2Fpasswd")

        application.state.pdf_inspection_service = _service(
            tmp_path / "corrupt", content=b"%PDF-1.7 altered bytes"
        )
        corrupt = await client.get(f"/api/pdf-inspections/{PDF_SHA256}")

        too_many = _Processor(
            PdfInspectionResult(True, MAX_PDF_PAGES + 1, 595.0, 842.0, False, (), (), (), ())
        )
        application.state.pdf_inspection_service = _service(
            tmp_path / "too-many", processor=too_many
        )
        limit = await client.get(f"/api/pdf-inspections/{PDF_SHA256}/pages/1.png")

        preview_cache = PdfInspectionCache(tmp_path / "expired", preview_ttl_seconds=0)
        expired_service = _service(tmp_path / "expired-service", references=(), cache=preview_cache)
        await expired_service.cache_preview_pdf(
            PDF,
            edition_id=UUID("00000000-0000-0000-0000-000000000020"),
            preview_input_hash="f" * 64,
        )
        application.state.pdf_inspection_service = expired_service
        expired = await client.get(f"/api/pdf-inspections/{PDF_SHA256}")

    assert metadata.status_code == 200
    assert metadata.json()["mapping"] == {
        "status": "not_available",
        "reason": "location_sidecar_unavailable",
        "items": [],
        "unmapped": [],
    }
    for response, expected_profile in ((page, "preview"), (thumbnail, "thumbnail")):
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["etag"].startswith('"')
        assert response.headers["x-pdf-sha256"] == PDF_SHA256
        assert response.headers["x-pdf-page"] in {"1", "2"}
        assert response.headers["x-pdf-profile"] == expected_profile
        assert response.content.startswith(b"\x89PNG")
    assert absent.status_code == 404 and absent.json()["detail"]["code"] == "absent"
    assert invalid_hash.status_code == 422
    assert invalid_hash.json()["detail"]["code"] == "hash_invalid"
    assert out_of_bounds.status_code == 416
    assert out_of_bounds.json()["detail"]["code"] == "page_out_of_bounds"
    assert path_attempt.status_code in {404, 422}
    if path_attempt.status_code == 422:
        assert path_attempt.json()["detail"]["code"] == "hash_invalid"
    assert corrupt.status_code == 422
    assert corrupt.json()["detail"]["code"] == "output_corrupt"
    assert limit.status_code == 413
    assert limit.json()["detail"]["code"] == "limit_exceeded"
    assert expired.status_code == 410
    assert expired.json()["detail"]["code"] == "preview_cache_expired"


@pytest.mark.asyncio
async def test_page_request_concurrency_is_globally_limited_to_two(tmp_path: Path) -> None:
    class SlowProcessor(_Processor):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.maximum_active = 0

        async def inspect(self, content: bytes) -> PdfInspectionResult:
            del content
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0.03)
            self.active -= 1
            return self.result

    processor = SlowProcessor()
    service = _service(tmp_path, processor=processor)
    await asyncio.gather(
        *(service.page_png(PDF_SHA256, 1, PdfInspectionProfile.PREVIEW) for _ in range(4))
    )
    assert processor.maximum_active == 2

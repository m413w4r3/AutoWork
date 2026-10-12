from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.typst_input_inspection import router
from cti_app.application.pdf_inspection import PdfRenderReference
from cti_app.application.typst_bundle import hash_bundle_contents
from cti_app.application.typst_input_inspection import (
    RenderInputInspectionError,
    RenderInputInspectionService,
)
from cti_app.application.typst_input_snapshot import (
    MAX_SNAPSHOT_FILE_BYTES,
    build_typst_input_snapshot,
    decode_typst_input_snapshot,
    preview_inspection_id,
    preview_snapshot_cache_key,
)
from cti_app.application.typst_rendering import (
    TemplateFile,
    TypstRenderSource,
    TypstTemplateBundle,
)
from cti_app.domain.typst_input_inspection import (
    RenderInputDiagnosticCategory,
    RenderInputInspectionErrorCode,
)

RENDER_ID = UUID("00000000-0000-4000-8000-000000000001")
SOURCE_BLOB_ID = UUID("00000000-0000-4000-8000-000000000002")
DATA_BLOB_ID = UUID("00000000-0000-4000-8000-000000000003")
SUBJECT_ID = "00000000-0000-4000-8000-000000000010"
PDF_SHA256 = "a" * 64
TARGET_ID = "target-diagram-1"


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _render_data(*, required_title: bool = True, media: bool = False) -> dict[str, object]:
    section = {
        "type": "synthesis",
        "blocks": [
            {"type": "diagram", "key": "attack-path", "target_id": TARGET_ID},
        ],
    }
    references = {
        "type": "references",
        "timeline": [{"text": "Reference", "source_urls": ["https://intel.example/source"]}],
        "sources": [{"url": "https://intel.example/source", "title": "Source"}],
        "blocks": [],
    }
    result: dict[str, object] = {
        "schema_version": "typst-publication-model-v20-block-location",
        "language": "fr",
        "content_sections": [references, section],
    }
    if required_title:
        result["title"] = "Sujet d'essai"
    if media:
        result["media"] = ["media/asset.svg"]
    return result


def _inputs(
    data: dict[str, object] | None = None,
    *,
    media_refs: tuple[object, ...] = (),
    source: bytes = b'#import "helpers.typ"\n#show: render\n',
    extra_template: bytes = b"#let render(body) = body\n",
) -> tuple[TypstTemplateBundle, TypstRenderSource, bytes]:
    payload = data if data is not None else _render_data()
    render_data_bytes = _json_bytes(payload)
    template_files = (
        TemplateFile("RENDERER/publication.typ", source),
        TemplateFile("RENDERER/helpers.typ", extra_template),
    )
    bundle = TypstTemplateBundle(
        template_version="publication-template-test-v1",
        sha256=hash_bundle_contents(
            tuple((item.relative_path, item.content) for item in template_files)
        ),
        files=template_files,
    )
    render_source = TypstRenderSource(
        source_bytes=source,
        source_sha256=hashlib.sha256(source).hexdigest(),
        render_data_bytes=render_data_bytes,
        render_data_sha256=hashlib.sha256(render_data_bytes).hexdigest(),
        media_refs=media_refs,  # type: ignore[arg-type]
    )
    snapshot = build_typst_input_snapshot(
        template_bundle=bundle,
        render_source=render_source,
        resolved_media={},
        render_id=str(RENDER_ID),
    )
    assert snapshot is not None
    return bundle, render_source, snapshot


def _reference(
    *,
    source_blob_id: UUID | None = SOURCE_BLOB_ID,
    render_data_blob_id: UUID | None = DATA_BLOB_ID,
    status: str = "succeeded",
    error_code: str | None = None,
    error_message: str | None = None,
    output_sha256: str | None = PDF_SHA256,
) -> PdfRenderReference:
    return PdfRenderReference(
        id=RENDER_ID,
        kind="publication_render",
        status=status,
        input_hash="b" * 64,
        renderer_version="publication-renderer-test-v1",
        template_sha256="c" * 64,
        compiler_version="0.15.1",
        font_bundle_version="font-bundle-test-v1",
        output_blob_id=None,
        output_sha256=output_sha256,
        output_byte_size=1234 if output_sha256 else None,
        source={
            "kind": "subject_publication",
            "subject_id": SUBJECT_ID,
            "artifact_id": "00000000-0000-4000-8000-000000000020",
            "artifact_version": 3,
            "artifact_input_hash": "d" * 64,
        },
        template_version="publication-template-test-v1",
        source_blob_id=source_blob_id,
        render_data_blob_id=render_data_blob_id,
        error_code=error_code,
        error_message=error_message,
    )


class _Repository:
    def __init__(self, render: PdfRenderReference | None) -> None:
        self.render = render
        self.reads = 0

    async def find_by_render_id(self, render_id: UUID) -> PdfRenderReference | None:
        self.reads += 1
        return self.render if self.render and render_id == self.render.id else None


class _BlobReader:
    def __init__(self, values: dict[UUID, bytes]) -> None:
        self.values = values
        self.reads = 0

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        self.reads += 1
        value = self.values[blob_id]
        if len(value) > max_bytes:
            raise ValueError("too large")
        return value


class _PdfInspection:
    def __init__(self) -> None:
        self.calls = 0

    async def metadata(self, pdf_sha256: str) -> dict[str, object]:
        self.calls += 1
        assert pdf_sha256 == PDF_SHA256
        return {
            "availability": "ready",
            "pdf": {"sha256": PDF_SHA256, "page_count": 2},
            "raster": {
                "page_png_url_template": f"/api/pdf-inspections/{PDF_SHA256}/pages/{{page}}.png"
            },
            "diagnostics": {
                "items": [{"code": "page_blank", "page": 2, "measurement": {"text_word_count": 0}}]
            },
            "mapping": {
                "status": "partial",
                "items": [
                    {
                        "target_id": TARGET_ID,
                        "target": {"subject_id": SUBJECT_ID, "artifact_id": "artifact-owner"},
                        "locations": [{"page": 1}],
                    },
                    {
                        "target_id": "target-from-another-render",
                        "target": {"subject_id": "another-subject"},
                        "locations": [{"page": 2}],
                    },
                ],
            },
        }


class _DerivedCache:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get_derived(self, key: str) -> bytes | None:
        return self.values.get(key)

    def put_derived(self, key: str, value: bytes) -> None:
        self.values[key] = value


class _CallCounter:
    def __init__(self) -> None:
        self.calls = 0

    async def compile(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.calls += 1
        raise AssertionError("inspection must not compile")

    async def run_model(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.calls += 1
        raise AssertionError("inspection must not call a model")


def _service(
    *,
    render: PdfRenderReference | None = None,
    source_snapshot: bytes | None = None,
    render_data_bytes: bytes | None = None,
    preview_cache: _DerivedCache | None = None,
) -> tuple[RenderInputInspectionService, _Repository, _BlobReader, _PdfInspection]:
    selected_render = render if render is not None else _reference()
    if source_snapshot is not None:
        snapshot_metadata = decode_typst_input_snapshot(source_snapshot)
        if snapshot_metadata is not None:
            selected_render = replace(
                selected_render,
                template_sha256=snapshot_metadata["template_sha256"],
            )
    repository = _Repository(selected_render)
    values = {}
    if source_snapshot is not None:
        values[SOURCE_BLOB_ID] = source_snapshot
    if render_data_bytes is not None:
        values[DATA_BLOB_ID] = render_data_bytes
    blobs = _BlobReader(values)
    pdf = _PdfInspection()
    service = RenderInputInspectionService(
        repository,
        blobs,
        pdf,  # type: ignore[arg-type]
        preview_cache or _DerivedCache(),
    )
    return service, repository, blobs, pdf


@pytest.mark.asyncio
async def test_persisted_inspection_lists_exact_inputs_links_pdf_and_never_executes() -> None:
    _, _, snapshot = _inputs()
    render_data = _json_bytes(_render_data())
    service, repository, blobs, pdf = _service(
        source_snapshot=snapshot,
        render_data_bytes=render_data,
    )
    calls = _CallCounter()

    metadata = await service.metadata(str(RENDER_ID))
    files = await service.files(str(RENDER_ID))
    entrypoint = next(item for item in files["files"] if item["path"] == "RENDERER/publication.typ")
    content = await service.file(str(RENDER_ID), entrypoint["file_id"], start_line=1, line_count=2)
    selected = await service.render_data(
        str(RENDER_ID), subject_id=UUID(SUBJECT_ID), block_key="attack-path"
    )
    diagnostics = await service.diagnostics(str(RENDER_ID))

    assert (
        metadata["source"]["entrypoint"]["sha256"]
        == hashlib.sha256(b'#import "helpers.typ"\n#show: render\n').hexdigest()
    )
    assert {item["path"] for item in files["files"]} == {
        "RENDERER/publication.typ",
        "RENDERER/helpers.typ",
    }
    assert content["content"] == '#import "helpers.typ"\n#show: render\n'
    assert selected["matches"][0]["value"]["key"] == "attack-path"
    assert selected["sha256"] == hashlib.sha256(render_data).hexdigest()
    assert metadata["pdf"]["sha256"] == PDF_SHA256
    assert metadata["pdf"]["page_count"] == 2
    assert metadata["pdf"]["canonical_blocks"] == [
        {
            "target_id": TARGET_ID,
            "target": {"subject_id": SUBJECT_ID, "artifact_id": "artifact-owner"},
            "locations": [{"page": 1}],
        }
    ]
    layout = next(item for item in diagnostics["diagnostics"] if item["code"] == "page_blank")
    assert layout["category"] is RenderInputDiagnosticCategory.LAYOUT_DEPENDENT
    assert repository.reads == 5
    assert blobs.reads == 10
    assert pdf.calls >= 2
    assert calls.calls == 0


@pytest.mark.asyncio
async def test_missing_required_json_field_is_a_canonical_data_diagnostic() -> None:
    _, _, snapshot = _inputs(_render_data(required_title=False))
    service, *_ = _service(
        source_snapshot=snapshot,
        render_data_bytes=_json_bytes(_render_data(required_title=False)),
    )

    result = await service.diagnostics(str(RENDER_ID))

    error = next(
        item
        for item in result["diagnostics"]
        if item["code"] == "render_data_required_field_missing"
    )
    assert error["category"] is RenderInputDiagnosticCategory.CANONICAL_DATA
    assert error["location"] == {"json_pointer": "/title"}
    assert error["suggested_channel"]["id"] == "content_revision"


@pytest.mark.asyncio
async def test_missing_asset_and_template_failure_are_typed_diagnostics() -> None:
    from cti_app.application.typst_rendering import TypstMediaRef
    from cti_app.domain.media_assets import MediaAssetKind

    media_ref = TypstMediaRef(
        asset_id=UUID("00000000-0000-4000-8000-000000000030"),
        expected_kind=MediaAssetKind.DIAGRAM_SVG,
        expected_mime_type="image/svg+xml",
        expected_sha256=None,
        expected_byte_size=None,
        media_path="media/diagram.svg",
    )
    _, _, asset_snapshot = _inputs(media_refs=(media_ref,))
    asset_service, *_ = _service(
        source_snapshot=asset_snapshot,
        render_data_bytes=_json_bytes(_render_data()),
    )
    asset_diagnostics = await asset_service.diagnostics(str(RENDER_ID))
    assert any(item["code"] == "asset_missing" for item in asset_diagnostics["diagnostics"])

    failed_ref = _reference(
        source_blob_id=None,
        render_data_blob_id=None,
        status="failed",
        error_code="publication_render_template_invalid",
        error_message="/home/private/work/renderer.typ:18:3: bad template",
        output_sha256=None,
    )
    template_service, *_ = _service(render=failed_ref)
    template_diagnostics = await template_service.diagnostics(str(RENDER_ID))
    template_error = next(
        item
        for item in template_diagnostics["diagnostics"]
        if item["code"] == "publication_render_template_invalid"
    )
    assert template_error["category"] is RenderInputDiagnosticCategory.TYPST_SOURCE
    assert template_error["suggested_channel"]["id"] == "template_code"
    assert "/home/private" not in str(template_diagnostics)


@pytest.mark.asyncio
async def test_compiler_error_location_is_relative_and_blob_hash_mismatch_is_typed() -> None:
    _, _, snapshot = _inputs()
    failed_ref = _reference(
        status="failed",
        error_code="typst_compilation_failed",
        error_message="/tmp/private/RENDERER/publication.typ:2:4: expected content",
        output_sha256=None,
    )
    compiler_service, *_ = _service(
        render=failed_ref,
        source_snapshot=snapshot,
        render_data_bytes=_json_bytes(_render_data()),
    )
    result = await compiler_service.diagnostics(str(RENDER_ID))
    error = next(
        item for item in result["diagnostics"] if item["code"] == "typst_compilation_failed"
    )
    assert error["category"] is RenderInputDiagnosticCategory.COMPILER
    assert error["location"] == {"file": "RENDERER/publication.typ", "line": 2, "column": 4}

    mismatch_service, *_ = _service(
        source_snapshot=snapshot,
        render_data_bytes=_json_bytes({**_render_data(), "title": "changed"}),
    )
    with pytest.raises(RenderInputInspectionError) as raised:
        await mismatch_service.render_data(str(RENDER_ID))
    assert raised.value.code is RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH


@pytest.mark.asyncio
async def test_unknown_render_invented_file_ids_traversal_and_bounds_are_controlled() -> None:
    _, _, snapshot = _inputs()
    service, *_ = _service(
        source_snapshot=snapshot,
        render_data_bytes=_json_bytes(_render_data()),
    )
    with pytest.raises(RenderInputInspectionError) as unknown:
        await service.metadata("00000000-0000-4000-8000-000000000099")
    assert unknown.value.code is RenderInputInspectionErrorCode.RENDER_NOT_FOUND
    for supplied in ("fake-file-id", "../publication.typ", "/etc/passwd", "C:/secret.typ"):
        with pytest.raises(RenderInputInspectionError) as missing:
            await service.file(str(RENDER_ID), supplied)
        assert missing.value.code is RenderInputInspectionErrorCode.FILE_NOT_IN_BUNDLE
    with pytest.raises(RenderInputInspectionError) as too_many:
        await service.file(str(RENDER_ID), "any", line_count=201)
    assert too_many.value.code is RenderInputInspectionErrorCode.SIZE_LIMIT_EXCEEDED
    assert MAX_SNAPSHOT_FILE_BYTES == 16 * 1024 * 1024


@pytest.mark.asyncio
async def test_preview_cache_supplies_exact_json_without_database_or_reexecution() -> None:
    bundle, render_source, _ = _inputs()
    preview_hash = "b" * 64
    cache = _DerivedCache()
    inspection_id = preview_inspection_id(preview_hash, PDF_SHA256)
    preview_snapshot = build_typst_input_snapshot(
        template_bundle=bundle,
        render_source=render_source,
        resolved_media={},
        render_id=inspection_id,
        preview_input_hash=preview_hash,
        pdf_sha256=PDF_SHA256,
    )
    assert preview_snapshot is not None
    cache.put_derived(preview_snapshot_cache_key(inspection_id), preview_snapshot)
    repository = _Repository(None)
    blobs = _BlobReader({})
    pdf = _PdfInspection()
    service = RenderInputInspectionService(repository, blobs, pdf, cache)  # type: ignore[arg-type]

    metadata = await service.preview_metadata(inspection_id)
    data = await service.render_data(inspection_id, preview=True)

    assert metadata["render"]["type"] == "edition_preview"
    assert metadata["render"]["identity"]["preview_input_hash"] == preview_hash
    assert data["json"] == _render_data()
    assert repository.reads == 0
    assert blobs.reads == 0


@pytest.mark.asyncio
async def test_http_routes_return_typed_not_found_without_compiler_or_model_calls() -> None:
    _, _, snapshot = _inputs()
    service, *_ = _service(
        source_snapshot=snapshot,
        render_data_bytes=_json_bytes(_render_data()),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.render_input_inspection_service = service
    counters = _CallCounter()
    app.state.typst_compiler = counters
    app.state.model_gateway = counters
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/api/render-inspections/00000000-0000-4000-8000-000000000099")
        unknown_file = await client.get(
            f"/api/render-inspections/{RENDER_ID}/files/invented-file-id"
        )
        listed = await client.get(f"/api/render-inspections/{RENDER_ID}/files")
        entrypoint = next(
            item for item in listed.json()["files"] if item["path"] == "RENDERER/publication.typ"
        )
        file_content = await client.get(
            f"/api/render-inspections/{RENDER_ID}/files/{entrypoint['file_id']}?start_line=1&line_count=1"
        )
        selected_data = await client.get(
            f"/api/render-inspections/{RENDER_ID}/render-data?pointer=/title"
        )
        diagnostics = await client.get(f"/api/render-inspections/{RENDER_ID}/diagnostics")
    assert missing.status_code == 404
    assert missing.json()["detail"] == {"code": "render_not_found"}
    assert unknown_file.status_code == 404
    assert unknown_file.json()["detail"] == {"code": "file_not_in_bundle"}
    assert file_content.status_code == 200
    assert file_content.json()["content"] == '#import "helpers.typ"\n'
    assert selected_data.json()["schema_version"] == "render-data-inspection-v1"
    assert selected_data.json()["schema"] == "typst-publication-model-v20-block-location"
    assert selected_data.json()["selection"] == {"pointer": "/title", "value": "Sujet d'essai"}
    assert diagnostics.status_code == 200
    assert counters.calls == 0


def test_snapshot_limits_reject_oversized_bundle_files_and_path_traversal() -> None:
    payload = _render_data()
    render_data_bytes = _json_bytes(payload)
    source = b"x" * (MAX_SNAPSHOT_FILE_BYTES + 1)
    files = (TemplateFile("RENDERER/publication.typ", source),)
    bundle = TypstTemplateBundle(
        "too-large",
        hash_bundle_contents(tuple((item.relative_path, item.content) for item in files)),
        files,
    )
    render_source = TypstRenderSource(
        source,
        hashlib.sha256(source).hexdigest(),
        render_data_bytes,
        hashlib.sha256(render_data_bytes).hexdigest(),
        (),
    )
    assert (
        build_typst_input_snapshot(
            template_bundle=bundle,
            render_source=render_source,
            resolved_media={},
        )
        is None
    )
    unsafe = (TemplateFile("../secret.typ", b"#show: render"),)
    unsafe_bundle = TypstTemplateBundle(
        "unsafe",
        hash_bundle_contents(tuple((item.relative_path, item.content) for item in unsafe)),
        unsafe,
    )
    unsafe_source = TypstRenderSource(
        unsafe[0].content,
        hashlib.sha256(unsafe[0].content).hexdigest(),
        render_data_bytes,
        hashlib.sha256(render_data_bytes).hexdigest(),
        (),
        entrypoint_relative_path="../secret.typ",
    )
    assert (
        build_typst_input_snapshot(
            template_bundle=unsafe_bundle,
            render_source=unsafe_source,
            resolved_media={},
        )
        is None
    )

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from cti_app.application.http_collection import (
    CollectionPolicy,
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.source_figure_inventory import (
    ArchivedFigureAsset,
    ArchivedFigureSource,
    SourceFigureInventory,
)
from cti_app.application.source_media_collection import (
    SourceMediaArchiveService,
    SourceMediaPolicy,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.source_media import SourceMediaReasonCode, SourceMediaRecord, SourceMediaStatus

PUBLIC_IP = "93.184.216.34"
SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000101")
DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000102")


def _png(width: int, height: int, *, compression: int = 6, seed: int = 0) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(
                (
                    (x * 13 + y * 7 + seed) % 256,
                    (x * 17 + y * 3 + seed * 3) % 256,
                    (x * 5 + y * 11 + seed * 17) % 256,
                )
            )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), compression))
        + chunk(b"IEND", b"")
    )


def _png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    )


class _MemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls = 0

    async def put(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        content = source.read()
        digest = hashlib.sha256(content).hexdigest()
        self.objects.setdefault((logical_bucket, digest), content)
        self.put_calls += 1
        return BlobDescriptor(digest, len(content), mime_type, logical_bucket)

    async def exists(self, descriptor: BlobDescriptor) -> bool:
        return (descriptor.logical_bucket, descriptor.sha256) in self.objects

    async def materialize(
        self, descriptor: BlobDescriptor, destination: Path
    ) -> Literal["existing"]:
        del descriptor, destination
        return "existing"

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes:
        content = self.objects[(descriptor.logical_bucket, descriptor.sha256)]
        if len(content) > max_bytes:
            raise ValueError("blob exceeds read limit")
        return content

    async def delete(self, descriptor: BlobDescriptor) -> None:
        self.objects.pop((descriptor.logical_bucket, descriptor.sha256), None)


class _BlobRepository:
    def __init__(self, state: _MemoryState) -> None:
        self.state = state

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None:
        return self.state.blobs.get((logical_bucket, sha256))

    async def add(self, record: BlobRecord) -> None:
        key = (record.descriptor.logical_bucket, record.descriptor.sha256)
        self.state.blobs.setdefault(key, record)


class _SourceMediaRepository:
    def __init__(self, state: _MemoryState) -> None:
        self.state = state

    async def list_for_subject_policy(
        self, subject_id: UUID, policy_sha256: str
    ) -> tuple[SourceMediaRecord, ...]:
        return tuple(
            record
            for record in self.state.media.values()
            if record.subject_id == subject_id and record.policy_sha256 == policy_sha256
        )

    async def add_if_absent(self, record: SourceMediaRecord) -> SourceMediaRecord:
        return self.state.media.setdefault(record.id, record)


@dataclass
class _MemoryState:
    blobs: dict[tuple[str, str], BlobRecord]
    media: dict[UUID, SourceMediaRecord]


class _UnitOfWork:
    def __init__(self, state: _MemoryState) -> None:
        self.blobs = _BlobRepository(state)
        self.source_media_candidates = _SourceMediaRepository(state)

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _UnitOfWorkFactory:
    def __init__(self) -> None:
        self.state = _MemoryState({}, {})

    def __call__(self) -> _UnitOfWork:
        return _UnitOfWork(self.state)


class _Resolver:
    async def resolve(self, hostname: str) -> tuple[str, ...]:
        del hostname
        return (PUBLIC_IP,)


class _Transport:
    def __init__(self, responses: dict[str, RawHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[PinnedHttpRequest] = []

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        self.requests.append(request)
        return self.responses.get(
            request.url,
            RawHttpResponse(404, {"content-type": "text/plain"}, b"missing"),
        )


def _response(content: bytes, *, mime: str = "image/png") -> RawHttpResponse:
    return RawHttpResponse(200, {"content-type": mime}, content)


def _source(
    html: str, *, url: str = "https://site.example/articles/report/index.html"
) -> ArchivedFigureSource:
    content = html.encode()
    return ArchivedFigureSource(
        source_document_id=DOCUMENT_ID,
        source_url=url,
        mime_type="text/html",
        blob_id=uuid4(),
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        content=content,
    )


def _service(
    responses: dict[str, RawHttpResponse],
    *,
    factory: _UnitOfWorkFactory | None = None,
    policy: SourceMediaPolicy | None = None,
    collector_policy: CollectionPolicy | None = None,
) -> tuple[SourceMediaArchiveService, _UnitOfWorkFactory, _MemoryBlobStore, _Transport]:
    uow_factory = factory or _UnitOfWorkFactory()
    store = _MemoryBlobStore()
    transport = _Transport(responses)
    service = SourceMediaArchiveService(
        uow_factory,
        SafeHttpCollector(transport, _Resolver(), collector_policy),
        store,
        policy=policy,
    )
    return service, uow_factory, store, transport


async def test_html_media_collection_excludes_boilerplate_and_keeps_provenance() -> None:
    figure = _png(400, 260)
    lazy_figure = _png(400, 260, seed=17)
    tiny = _png(1, 1)
    html = """<!doctype html><html><body>
      <header><img alt="Header artwork" src="/header-art.png"></header>
      <nav><img alt="Products menu" src="/menu.png"></nav>
      <main><img alt="Site logo" src="/logo.png"><h2>Credential theft analysis</h2>
        <figure><a href="/evidence/graph"><img alt="Network activity" class="chart"
          src="/fallback.png" srcset="/graph-small.png 320w, /graph-large.png 1200w"></a>
          <figcaption>Outbound connections by day</figcaption></figure>
        <img alt="Lazy chart" data-src="../assets/lazy-chart.png">
        <img alt="Tiny chart" src="/tiny-chart.png">
      </main>
      <footer><img alt="Footer logo" src="/footer.png"></footer>
    </body></html>"""
    responses = {
        "https://site.example/graph-large.png": RawHttpResponse(
            302,
            {"location": "https://cdn.site.example/graph.png"},
            b"",
        ),
        "https://cdn.site.example/graph.png": _response(figure),
        "https://site.example/articles/assets/lazy-chart.png": _response(lazy_figure),
        "https://site.example/tiny-chart.png": _response(tiny),
    }
    service, _factory, _store, transport = _service(responses)

    records = await service.collect(SUBJECT_ID, (_source(html),))

    by_alt = {record.alt_text: record for record in records}
    assert by_alt["Site logo"].status is SourceMediaStatus.EXCLUDED_BY_RULE
    assert by_alt["Site logo"].reason_code is SourceMediaReasonCode.BOILERPLATE_PATTERN
    assert by_alt["Header artwork"].reason_code is SourceMediaReasonCode.NAVIGATION_LANDMARK
    assert by_alt["Products menu"].reason_code is SourceMediaReasonCode.NAVIGATION_LANDMARK
    assert by_alt["Footer logo"].reason_code is SourceMediaReasonCode.NAVIGATION_LANDMARK
    assert by_alt["Tiny chart"].reason_code is SourceMediaReasonCode.TRACKING_PIXEL
    assert by_alt["Network activity"].status is SourceMediaStatus.ACCEPTED_FOR_REVIEW
    assert by_alt["Network activity"].requested_url == "https://site.example/graph-large.png"
    assert by_alt["Network activity"].final_url == "https://cdn.site.example/graph.png"
    assert by_alt["Network activity"].width == 400
    assert by_alt["Network activity"].height == 260
    assert by_alt["Network activity"].caption_text == "Outbound connections by day"
    assert by_alt["Network activity"].nearby_heading_text == "Credential theft analysis"
    assert by_alt["Network activity"].anchor == "/evidence/graph"
    assert by_alt["Network activity"].dom_locator.endswith("img[1]")
    assert (
        by_alt["Network activity"].collection_diagnostics["decoded_sha256"]
        == hashlib.sha256(figure).hexdigest()
    )
    assert by_alt["Network activity"].collection_diagnostics["redirect_chain"] == [
        "https://cdn.site.example/graph.png"
    ]
    assert {request.url for request in transport.requests} == {
        "https://site.example/graph-large.png",
        "https://cdn.site.example/graph.png",
        "https://site.example/articles/assets/lazy-chart.png",
        "https://site.example/tiny-chart.png",
    }

    assets = tuple(
        ArchivedFigureAsset(
            source_document_id=record.source_document_id,
            source_url=record.requested_url,
            blob_id=record.blob_id,
            sha256=record.sha256,
            mime_type=record.mime_type,
            byte_size=record.byte_size,
        )
        for record in records
        if record.blob_id and record.sha256 and record.mime_type and record.byte_size is not None
    )
    inventory = SourceFigureInventory().inventory(
        (_source(html),),
        assets,
        media_candidates=records,
        policy_sha256=service.policy_sha256,
    )
    accepted = inventory.accepted
    assert {figure.locator.figure_label for figure in accepted} == {
        "Network activity",
        "Lazy chart",
    }
    assert inventory.policy_sha256 == service.policy_sha256
    assert any("Outbound connections by day" in figure.provenance for figure in accepted)


async def test_pdf_embedded_image_is_archived_and_page_crop_need_is_recorded() -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=500, height=400)
    pixels = bytes((index * 13) % 256 for index in range(400 * 260 * 3))
    image_object = DecodedStreamObject()
    image_object.set_data(pixels)
    image_object.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(400),
            NameObject("/Height"): NumberObject(260),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    image_ref = writer._add_object(image_object)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image_ref}),
        }
    )
    page_content = DecodedStreamObject()
    page_content.set_data(b"q 400 0 0 260 0 0 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(page_content)
    pdf_stream = BytesIO()
    writer.write(pdf_stream)
    content = pdf_stream.getvalue()
    source = ArchivedFigureSource(
        source_document_id=DOCUMENT_ID,
        source_url="https://site.example/report.pdf",
        mime_type="application/pdf",
        blob_id=uuid4(),
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        content=content,
    )
    service, factory, store, _transport = _service({})

    records = await service.collect(SUBJECT_ID, (source,))

    assert any(record.status is SourceMediaStatus.ACCEPTED_FOR_REVIEW for record in records), [
        (record.status, record.reason_code, record.collection_diagnostics, record.byte_size)
        for record in records
    ]
    excerpt = next(
        record for record in records if record.status is SourceMediaStatus.PAGE_EXCERPT_NEEDED
    )
    assert excerpt.reason_code is SourceMediaReasonCode.PDF_PAGE_EXCERPT_NEEDED
    assert excerpt.page == 1
    assert excerpt.page_bbox == {"left": 0.0, "bottom": 0.0, "right": 500.0, "top": 400.0}
    accepted = next(
        record for record in records if record.status is SourceMediaStatus.ACCEPTED_FOR_REVIEW
    )
    assert accepted.blob_id is not None
    assert accepted.mime_type == "image/png"
    assert (accepted.width, accepted.height) == (400, 260)
    assert accepted.sha256 is not None
    assert (
        accepted.sha256
        == hashlib.sha256(store.objects[("source-media-candidates", accepted.sha256)]).hexdigest()
    )
    assert len(factory.state.blobs) == 1


async def test_unreachable_unsafe_and_over_limit_images_have_explicit_outcomes() -> None:
    image = _png(400, 260)
    html = """<img alt="Unavailable chart" src="https://site.example/missing.png">
      <img alt="Unsafe chart" src="http://127.0.0.1/private.png">
      <img alt="Large chart" src="https://site.example/large.png">"""
    responses = {
        "https://site.example/large.png": _response(image),
    }
    policy = SourceMediaPolicy(maximum_bytes=1024)
    service, factory, _store, transport = _service(responses, policy=policy)

    records = await service.collect(SUBJECT_ID, (_source(html),))

    by_alt = {record.alt_text: record for record in records}
    assert by_alt["Unavailable chart"].status is SourceMediaStatus.COLLECTION_FAILED
    assert by_alt["Unavailable chart"].reason_code is SourceMediaReasonCode.COLLECTION_UNAVAILABLE
    assert by_alt["Unsafe chart"].reason_code is SourceMediaReasonCode.UNSAFE_DESTINATION
    assert by_alt["Large chart"].reason_code is SourceMediaReasonCode.EXCEEDS_MAXIMUM_BYTES
    assert not any(request.url.startswith("http://127.0.0.1") for request in transport.requests)
    assert not factory.state.blobs
    assert by_alt["Unavailable chart"].collection_diagnostics["http_status"] == 404


@pytest.mark.parametrize(("width", "height"), [(10_001, 200), (8_000, 6_000)])
async def test_images_with_excessive_dimensions_are_excluded_before_archiving(
    width: int, height: int
) -> None:
    service, factory, store, _transport = _service(
        {"https://site.example/large-dimensions.png": _response(_png_header(width, height))}
    )

    (record,) = await service.collect(
        SUBJECT_ID,
        (_source('<img alt="Oversized chart" src="/large-dimensions.png">'),),
    )

    assert record.status is SourceMediaStatus.EXCLUDED_BY_RULE
    assert record.reason_code is SourceMediaReasonCode.IMAGE_TOO_LARGE_DIMENSIONS
    assert (record.width, record.height) == (width, height)
    assert not factory.state.blobs
    assert not store.objects


async def test_collector_wire_size_limit_is_recorded_as_collection_diagnostic() -> None:
    image = _png(400, 260)
    service, _factory, _store, _transport = _service(
        {"https://site.example/chart.png": _response(image)},
        collector_policy=CollectionPolicy(max_download_bytes=256, max_expanded_bytes=512),
    )
    source = _source('<img alt="Large chart" src="/chart.png">')

    (record,) = await service.collect(SUBJECT_ID, (source,))

    assert record.status is SourceMediaStatus.COLLECTION_FAILED
    assert record.reason_code is SourceMediaReasonCode.COLLECTION_SIZE_LIMIT
    assert record.collection_diagnostics["collector_reason_code"] == "size_limit"


async def test_identical_bytes_keep_two_provenances_and_one_blob_and_replay_is_idempotent() -> None:
    image = _png(400, 260)
    html = """<figure><img alt="First chart" src="/first.png"></figure>
      <figure><img alt="Second chart" src="/second.png"></figure>"""
    responses = {
        "https://site.example/first.png": _response(image),
        "https://site.example/second.png": _response(image),
    }
    service, factory, store, transport = _service(responses)
    source = _source(html)

    first = await service.collect(SUBJECT_ID, (source,))
    second = await service.collect(SUBJECT_ID, (source,))

    assert len(first) == len(second) == 2
    assert first == second
    assert len(factory.state.media) == 2
    assert len(factory.state.blobs) == 1
    assert len(store.objects) == 1
    assert len(transport.requests) == 2
    assert len({record.blob_id for record in first}) == 1
    assert {record.requested_url for record in first} == {
        "https://site.example/first.png",
        "https://site.example/second.png",
    }
    assert sum(record.status is SourceMediaStatus.ACCEPTED_FOR_REVIEW for record in first) == 1
    assert (
        sum(record.reason_code is SourceMediaReasonCode.DUPLICATE_EXACT_HASH for record in first)
        == 1
    )


async def test_policy_version_change_changes_candidate_identity_and_decision_hash() -> None:
    image = _png(400, 260)
    source = _source('<img alt="Traffic chart" src="/chart.png">')
    responses = {"https://site.example/chart.png": _response(image)}
    service_v1, factory, _store, _transport = _service(responses)
    first = await service_v1.collect(SUBJECT_ID, (source,))
    service_v2, _, _, _ = _service(
        responses,
        factory=factory,
        policy=SourceMediaPolicy(version="source-media-exclusion-v3"),
    )

    second = await service_v2.collect(SUBJECT_ID, (source,))

    assert first[0].id != second[0].id
    assert first[0].policy_sha256 != second[0].policy_sha256
    assert second[0].policy_version == "source-media-exclusion-v3"
    inventories = []
    for service, records in ((service_v1, first), (service_v2, second)):
        assets = tuple(
            ArchivedFigureAsset(
                source_document_id=record.source_document_id,
                source_url=record.requested_url,
                blob_id=record.blob_id,
                sha256=record.sha256,
                mime_type=record.mime_type,
                byte_size=record.byte_size,
            )
            for record in records
            if record.blob_id
            and record.sha256
            and record.mime_type
            and record.byte_size is not None
        )
        inventories.append(
            SourceFigureInventory().inventory(
                (source,),
                assets,
                media_candidates=records,
                policy_sha256=service.policy_sha256,
            )
        )
    assert inventories[0].content_hash() != inventories[1].content_hash()


async def test_png_perceptual_hash_excludes_reencoded_duplicate() -> None:
    source = _source(
        '<img alt="First chart" src="/first.png"><img alt="Reencoded chart" src="/second.png">'
    )
    image_fast = _png(400, 260, compression=1)
    image_small = _png(400, 260, compression=9)
    assert image_fast != image_small
    service, _factory, _store, _transport = _service(
        {
            "https://site.example/first.png": _response(image_fast),
            "https://site.example/second.png": _response(image_small),
        }
    )

    records = await service.collect(SUBJECT_ID, (source,))

    assert sum(record.status is SourceMediaStatus.ACCEPTED_FOR_REVIEW for record in records) == 1
    assert (
        sum(
            record.reason_code is SourceMediaReasonCode.DUPLICATE_PERCEPTUAL_HASH
            for record in records
        )
        == 1
    )

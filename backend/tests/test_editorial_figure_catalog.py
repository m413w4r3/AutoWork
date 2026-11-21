from __future__ import annotations

import hashlib
import struct
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.application import editorial_figure_catalog as editorial_figure_catalog_module
from cti_app.application.editorial_figure_catalog import (
    EditorialFigureCatalogCache,
    EditorialFigureCatalogItem,
    EditorialFigureCatalogService,
    FigureCatalogSource,
    _CachedFigureCatalog,
    classify_source_figure,
    opaque_figure_id,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.production_editorial_enrichment import (
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.figure_thumbnails import (
    FigureThumbnailCache,
    FigureThumbnailError,
    prepare_figure_content,
)


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    rows = b"".join(
        b"\x00" + b"".join(bytes((x % 256, y % 256, (x + y) % 256)) for x in range(width))
        for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class _MissingSubjectRepository:
    async def get(self, _subject_id: UUID) -> None:
        return None


def _missing_subject_uow_factory() -> UnitOfWorkFactory:
    @asynccontextmanager
    async def open_uow() -> AsyncIterator[UnitOfWork]:
        yield cast(UnitOfWork, SimpleNamespace(subjects=_MissingSubjectRepository()))

    return cast(UnitOfWorkFactory, open_uow)


def _figure_route_app(monkeypatch: pytest.MonkeyPatch, blob_root: Path) -> FastAPI:
    settings = Settings(
        _env_file=None,
        app_env="development",
        api_bind_host="127.0.0.1",
        editorial_proposal_accept_enabled=True,
        production_editorial_resource_search_enabled=False,
    )
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(editorial_workspace_api, "get_settings", lambda: settings)
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    app = api_main.create_app()
    app.state.uow_factory = _missing_subject_uow_factory()
    app.state.blob_store = FilesystemBlobStore(blob_root)
    assert getattr(app.state, "production_artifact_store", None) is None
    return app


def _request_for_app(app: FastAPI, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": path,
            "headers": [],
        }
    )


def _figure(decision: SourceFigureDecision, reason: str) -> ResolvedSourceFigureV1:
    document_id = UUID("10000000-0000-0000-0000-000000000001")
    source = "https://publisher.example/article"
    locator = SourceFigureLocatorV1(figure_label="Figure 1")
    return ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=document_id,
            sha256=None,
            source=source,
            locator=locator,
        ),
        blob_id=None,
        sha256=None,
        mime_type=None,
        byte_size=None,
        source_document_id=document_id,
        source=source,
        provenance="source_document",
        locator=locator,
        decision=decision,
        decision_reason=reason,
    )


def _thumbnail_route_app(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    mime_type: str,
) -> tuple[FastAPI, str]:
    subject_id, run_id, document_id, blob_id, figure_id = (uuid4() for _ in range(5))
    sha256 = hashlib.sha256(content).hexdigest()
    item = SimpleNamespace(
        figure_id=str(figure_id),
        mime_type=mime_type,
        _blob_id=blob_id,
        _sha256=sha256,
        _byte_size=len(content),
        _run_id=run_id,
        _source_document_id=document_id,
    )

    class _Catalog:
        async def resolve(self, *, subject_id: UUID, figure_id: UUID) -> object:
            assert item.figure_id == str(figure_id)
            return item

    class _Repository:
        def __init__(self, expected_id: UUID, value: object) -> None:
            self.expected_id = expected_id
            self.value = value

        async def get(self, value_id: UUID) -> object | None:
            return self.value if value_id == self.expected_id else None

    class _Runs:
        async def get_current_for_subject(self, value_id: UUID) -> object | None:
            return SimpleNamespace(id=run_id) if value_id == subject_id else None

    blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=sha256,
            size=len(content),
            mime_type=mime_type,
            logical_bucket="synthetic-figure-test",
        )
    )
    uow = SimpleNamespace(
        subjects=_Repository(subject_id, SimpleNamespace(id=subject_id)),
        production_runs=_Runs(),
        source_documents=_Repository(
            document_id,
            SimpleNamespace(id=document_id, subject_id=subject_id, source_collection_id=None),
        ),
        blobs=_Repository(blob_id, blob),
    )

    @asynccontextmanager
    async def uow_factory():
        yield uow

    class _ArtifactStore:
        async def read_bytes(self, value_id: UUID, *, max_bytes: int | None = None) -> bytes:
            assert value_id == blob_id
            assert max_bytes is None or len(content) <= max_bytes
            return content

    monkeypatch.setattr(
        editorial_workspace_api,
        "_figure_catalog_service",
        lambda _request: _Catalog(),
    )
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = _ArtifactStore()
    return (
        app,
        f"/api/subjects/{subject_id}/editor/figures/{figure_id}/thumbnail",
    )


@pytest.mark.asyncio
async def test_catalog_route_builds_real_services_without_lifespan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subject_id = uuid4()

    app = _figure_route_app(monkeypatch, tmp_path / "catalog-blobs")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.get(f"/api/subjects/{subject_id}/editor/figures")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "editorial_figure_not_found"
    assert isinstance(app.state.production_artifact_store, ProductionArtifactStore)
    assert isinstance(
        editorial_workspace_api._figure_catalog_service(
            _request_for_app(app, f"/api/subjects/{subject_id}/editor/figures")
        ),
        EditorialFigureCatalogService,
    )


@pytest.mark.asyncio
async def test_figure_proposal_route_builds_real_catalog_without_lifespan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subject_id = uuid4()
    occurrence_id = uuid4()

    app = _figure_route_app(monkeypatch, tmp_path / "proposal-blobs")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{subject_id}/editor/blocks/{occurrence_id}/figure-proposals",
            headers={"Idempotency-Key": "figure-proposal-unit-test-001"},
            json={
                "snapshot_id": str(uuid4()),
                "anchor_block_id": str(occurrence_id),
                "figures": [{"figure_id": str(uuid4()), "caption": "Synthetic figure", "order": 0}],
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "editorial_figure_not_found"
    assert isinstance(app.state.production_artifact_store, ProductionArtifactStore)


def test_opaque_figure_id_is_scoped_to_subject_and_current_run() -> None:
    figure_id = uuid4()
    subject_id = uuid4()
    run_id = uuid4()

    public_id = opaque_figure_id(subject_id, run_id, figure_id)

    assert public_id != str(figure_id)
    assert public_id != opaque_figure_id(uuid4(), run_id, figure_id)
    assert public_id != opaque_figure_id(subject_id, uuid4(), figure_id)


def test_ineligible_figure_exposes_reason_and_is_not_selectable() -> None:
    projected = classify_source_figure(
        _figure(SourceFigureDecision.REJECTED, "figure_exceeds_byte_limit"),
        already_used=False,
        not_selected=False,
    )

    assert projected.status == "ineligible"
    assert projected.selectable is False
    assert projected.exclusion_reason == "Le fichier dépasse la taille maximale autorisée."


def test_pending_figure_is_never_selectable() -> None:
    projected = classify_source_figure(
        _figure(SourceFigureDecision.PENDING, "image_not_in_local_archive"),
        already_used=False,
        not_selected=False,
    )

    assert projected.status == "file_unavailable"
    assert projected.selectable is False


@pytest.mark.asyncio
async def test_thumbnail_route_serves_fixed_safe_svg_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = (
        b'<svg xmlns="http://www.w3.org/2000/svg" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink" width="120" height="80" '
        b"onload=\"fetch('https://attacker.example/load')\">"
        b"<script>fetch('https://attacker.example/script')</script>"
        b'<image href="https://attacker.example/image.png"/>'
        b'<image xlink:href="file:///etc/passwd"/>'
        b'<use href="https://attacker.example/use.svg#x"/>'
        b"<foreignObject><div>unsafe</div></foreignObject>"
        b'<style>@import url("https://attacker.example/style.css");'
        b"rect{fill:url(data:text/css,unsafe)}</style>"
        b'<rect style="fill:url(https://attacker.example/paint.svg#x)" '
        b'width="120" height="80" fill="#123456"/>'
        b"</svg>"
    )

    app, path = _thumbnail_route_app(monkeypatch, hostile, "image/svg+xml")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml; charset=utf-8"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'none'; sandbox"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    body = response.content.lower()
    for forbidden in (
        b"script",
        b"onload",
        b"attacker.example",
        b"file:",
        b"foreignobject",
        b"xlink",
        b"href",
        b"@import",
        b"url(",
        b"data:",
    ):
        assert forbidden not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile",
    [
        b"<!DOCTYPE svg [<!ENTITY x SYSTEM 'file:///etc/passwd'>]>"
        b"<svg xmlns='http://www.w3.org/2000/svg'><text>&x;</text></svg>",
        b"<!DOCTYPE svg [<!ENTITY a '1234567890'><!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;'>"
        b"<!ENTITY c '&b;&b;&b;&b;&b;&b;&b;&b;'>]><svg>&c;</svg>",
    ],
)
async def test_thumbnail_route_never_returns_xml_entities(
    monkeypatch: pytest.MonkeyPatch, hostile: bytes
) -> None:
    app, path = _thumbnail_route_app(monkeypatch, hostile, "image/svg+xml")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "figure_thumbnail_unavailable"
    assert b"file:///etc/passwd" not in response.content
    assert b"1234567890" not in response.content


def test_raster_response_keeps_archived_bytes_and_uses_fixed_mime() -> None:
    content = _png(640, 360)

    prepared = prepare_figure_content(content, "image/png")

    assert prepared.content == content
    assert prepared.media_type == "image/png"
    assert prepared.content_security_policy == "default-src 'none'"


def test_unsupported_raster_mime_is_rejected() -> None:
    with pytest.raises(FigureThumbnailError):
        prepare_figure_content(b"<html>unsafe</html>", "text/html")


def _cached_catalog() -> _CachedFigureCatalog:
    item = EditorialFigureCatalogItem(
        figure_id=str(uuid4()),
        original_name="Synthetic figure",
        original_caption=None,
        alt_text=None,
        source=FigureCatalogSource(source_id=str(uuid4()), title=None, url=None),
        page=None,
        section=None,
        context_before=None,
        context_after=None,
        width=320,
        height=180,
        mime_type="image/png",
        status="available",
        selectable=True,
        exclusion_reason=None,
        already_used=False,
        _source_figure_id=uuid4(),
        _source_document_id=uuid4(),
        _blob_id=uuid4(),
        _sha256="0" * 64,
        _byte_size=128,
        _run_id=uuid4(),
    )
    return _CachedFigureCatalog(
        items=(item,),
        access_extraction=SimpleNamespace(sources=()),  # type: ignore[arg-type]
        effective_tlp=TLP.AMBER,
    )


def test_catalog_cache_is_lru_bounded_and_expires_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr(editorial_figure_catalog_module, "monotonic", lambda: now)
    cache = EditorialFigureCatalogCache()
    keys = [(uuid4(), uuid4(), str(index)) for index in range(65)]
    value = _cached_catalog()

    for key in keys:
        cache.put(key, value)

    assert cache.get(keys[0]) is None
    assert cache.get(keys[-1]) == value
    now = 31.0
    assert cache.get(keys[-1]) is None


def test_thumbnail_cache_uses_content_hash_and_requested_size() -> None:
    cache = FigureThumbnailCache()
    prepared = prepare_figure_content(_png(10, 10), "image/png")

    cache.put("a" * 64, 320, "image/png", prepared)

    assert cache.get("a" * 64, 320, "image/png") == prepared
    assert cache.get("a" * 64, 1024, "image/png") is None

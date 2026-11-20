from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request

from cti_app.api import main as api_main
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProductionRunStatus,
)
from cti_app.domain.production_extraction import production_extraction_to_json
from tests.integration.editorial.test_proposal_adoption_postgres import _seed, _Seeded

pytestmark = pytest.mark.integration

_CANONICAL_BUCKET = "production-artifacts-canonical"
_SOURCE_BUCKET = "source-figure-authorization-test"
_IMAGE_URL = "https://publisher.example/report/map.png"
_PRIVATE_TEXT = "SYNTHETIC_PRIVATE_FIGURE_CONTEXT_94C1"


def _png(width: int, height: int, salt: int = 0) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    rows = b"".join(
        b"\x00" + b"".join(bytes((x % 256, y % 256, (x + y + salt) % 256)) for x in range(width))
        for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


async def _archive_figure_sources(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
    *,
    source_tlp: TLP = TLP.RED,
    with_collection: bool = False,
) -> tuple[UUID, tuple[UUID, ...], tuple[str, ...], UUID | None]:
    source_id = seeded.extraction.sources[0].source_document_id
    source_url = seeded.extraction.sources[0].canonical_url
    html = (
        "<html><body><h2>Infrastructure</h2>"
        f'<figure><img src="{_IMAGE_URL}" alt="Carte des flux">'
        f"<figcaption>Carte originale {_PRIVATE_TEXT}</figcaption></figure>"
        f"<p>{_PRIVATE_TEXT} contexte de la figure.</p></body></html>"
    ).encode()
    png = _png(800, 450, seeded.subject.id.int % 251)
    image_id = uuid4()
    now = datetime.now(UTC)
    collection = (
        SourceCollection(
            subject_id=seeded.subject.id,
            edition_id=seeded.subject.edition_id,
            requested_url=source_url,
            proposed_role=SourceRole.PRIMARY,
            source_tlp=source_tlp,
            external_llm_allowed=False,
            do_not_submit=True,
            state=CollectionState.ARCHIVED,
        )
        if with_collection
        else None
    )

    async with uow_factory() as uow:
        if collection is not None:
            assert await uow.source_collections.add_if_absent(collection)
        source_blob = await uow.blobs.add_if_absent(
            BlobRecord(
                descriptor=BlobDescriptor(
                    sha256=hashlib.sha256(html).hexdigest(),
                    size=len(html),
                    mime_type="text/html",
                    logical_bucket=_SOURCE_BUCKET,
                )
            )
        )
        image_blob = await uow.blobs.add_if_absent(
            BlobRecord(
                descriptor=BlobDescriptor(
                    sha256=hashlib.sha256(png).hexdigest(),
                    size=len(png),
                    mime_type="image/png",
                    logical_bucket=_SOURCE_BUCKET,
                )
            )
        )
        extraction_bytes = ProductionArtifactStore.canonical_json_bytes(
            production_extraction_to_json(seeded.extraction)
        )
        extraction_blob = await uow.blobs.add_if_absent(
            BlobRecord(
                descriptor=BlobDescriptor(
                    sha256=hashlib.sha256(extraction_bytes).hexdigest(),
                    size=len(extraction_bytes),
                    mime_type="application/json",
                    logical_bucket=_CANONICAL_BUCKET,
                )
            )
        )
        await uow.production_input_snapshots.add(seeded.production_input)
        await uow.source_documents.add(
            SourceDocument(
                id=source_id,
                subject_id=seeded.subject.id,
                blob_id=source_blob.id,
                original_name="synthetic-report.html",
                origin="synthetic_fixture",
                acquired_at=now,
                license_restriction=None,
                tlp=source_tlp,
                do_not_submit=True,
                external_llm_allowed=False,
                title="Synthetic archived report",
                final_url=source_url,
                detected_mime_type="text/html",
                source_collection_id=collection.id if collection is not None else None,
            )
        )
        await uow.source_documents.add(
            SourceDocument(
                id=image_id,
                subject_id=seeded.subject.id,
                blob_id=image_blob.id,
                original_name="map.png",
                origin="synthetic_fixture",
                acquired_at=now,
                license_restriction=None,
                tlp=source_tlp,
                do_not_submit=True,
                external_llm_allowed=False,
                final_url=_IMAGE_URL,
                detected_mime_type="image/png",
            )
        )
        await uow.production_artifacts.append(
            ProductionArtifact(
                production_run_id=seeded.run.id,
                subject_id=seeded.subject.id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=1,
                input_hash=seeded.production_input.input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=extraction_blob.id,
            )
        )
        await uow.commit()

    seeded.artifact_store.blobs[source_blob.id] = html
    seeded.artifact_store.blobs[image_blob.id] = png
    seeded.artifact_store.blobs[extraction_blob.id] = extraction_bytes
    return (
        source_id,
        (source_blob.id, image_blob.id, extraction_blob.id),
        (
            source_blob.descriptor.object_key,
            image_blob.descriptor.object_key,
            extraction_blob.descriptor.object_key,
        ),
        collection.id if collection is not None else None,
    )


def _app(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    require_token: bool = False,
) -> FastAPI:
    settings = Settings(
        _env_file=None,
        operator_api_token=(
            "synthetic-figure-test-token-0123456789abcdef" if require_token else None
        ),
    )
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    app = api_main.create_app()
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = seeded.artifact_store

    @app.get("/api/_test-figure-identity")
    async def read_actor(request: Request) -> dict[str, str]:
        return {"actor_id": request.state.identity.actor_id}

    return app


async def test_figure_catalog_scopes_subject_run_and_actor(
    uow_factory: UnitOfWorkFactory,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=0, seed_discovery_lineage=True)
    source_id, blob_ids, object_keys, _collection_id = await _archive_figure_sources(
        seeded, uow_factory
    )
    other_subject_seeded = await _seed(uow_factory, proposal_count=0, seed_discovery_lineage=True)
    await _archive_figure_sources(other_subject_seeded, uow_factory)
    seeded.artifact_store.blobs.update(other_subject_seeded.artifact_store.blobs)

    read_counts: dict[UUID, int] = {}
    original_read_bytes = seeded.artifact_store.read_bytes

    async def count_blob_read(blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        read_counts[blob_id] = read_counts.get(blob_id, 0) + 1
        return await original_read_bytes(blob_id, max_bytes=max_bytes)

    monkeypatch.setattr(seeded.artifact_store, "read_bytes", count_blob_read)
    unauthenticated_app = _app(seeded, uow_factory, monkeypatch, require_token=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unauthenticated_app),
        base_url="http://editorial.test",
    ) as client:
        refused = await client.get("/api/_test-figure-identity")
    assert refused.status_code == 401, refused.json()
    assert refused.json()["detail"]["code"] == "operator_token_required"

    app = _app(seeded, uow_factory, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        accepted_identity = await client.get("/api/_test-figure-identity")
        assert accepted_identity.status_code == 200, accepted_identity.json()
        assert accepted_identity.json() == {"actor_id": "dev-analyst"}
        path = f"/api/subjects/{seeded.subject.id}/editor/figures"
        response = await client.get(path)
        assert response.status_code == 200, response.json()
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        payload = response.json()
        assert payload["available_count"] == 1
        assert len(payload["items"]) == 1
        item = payload["items"][0]
        assert item["source"]["source_id"] == str(source_id)
        assert item["source"]["url"] is None
        assert item["selectable"] is True
        assert _PRIVATE_TEXT in item["original_caption"] or _PRIVATE_TEXT in (
            item["context_before"] or ""
        )
        body = response.text
        assert "blob_id" not in body
        assert "minio" not in body.casefold()
        assert _SOURCE_BUCKET not in body
        assert "map.png" not in body
        for blob_id in blob_ids:
            assert str(blob_id) not in body
        for object_key in object_keys:
            assert object_key not in body

        search_query = _PRIVATE_TEXT
        filtered = await client.get(
            path,
            params={
                "status": "available",
                "source": str(source_id),
                "q": search_query,
                "limit": 1,
            },
        )
        assert filtered.status_code == 200, filtered.json()
        assert filtered.json()["total"] == 1
        assert len(filtered.json()["items"]) == 1
        source_reads_after_first_page = read_counts.get(blob_ids[0], 0)
        image_reads_after_first_page = read_counts.get(blob_ids[1], 0)
        second_page = await client.get(path)
        assert second_page.status_code == 200, second_page.json()
        assert read_counts.get(blob_ids[0], 0) == source_reads_after_first_page
        assert read_counts.get(blob_ids[1], 0) == image_reads_after_first_page
        paged = await client.get(path, params={"offset": 1, "limit": 1})
        assert paged.status_code == 200, paged.json()
        assert paged.json()["total"] == 1
        assert paged.json()["items"] == []

        thumbnail = await client.get(f"{path}/{item['figure_id']}/thumbnail")
        assert thumbnail.status_code == 200, thumbnail.text
        assert thumbnail.headers["content-type"] == "image/png"
        assert thumbnail.headers["cache-control"] == "private, no-store"
        assert thumbnail.headers["x-content-type-options"] == "nosniff"
        assert thumbnail.headers["content-security-policy"] == "default-src 'none'"
        assert thumbnail.content == seeded.artifact_store.blobs[blob_ids[1]]
        assert read_counts.get(blob_ids[0], 0) == source_reads_after_first_page
        assert read_counts.get(blob_ids[1], 0) == image_reads_after_first_page + 1
        thumbnail_replay = await client.get(f"{path}/{item['figure_id']}/thumbnail")
        assert thumbnail_replay.status_code == 200
        assert thumbnail_replay.content == thumbnail.content
        assert read_counts.get(blob_ids[1], 0) == image_reads_after_first_page + 1

        preview = await client.get(f"{path}/{item['figure_id']}/preview")
        assert preview.status_code == 200, preview.json()
        assert preview.headers["cache-control"] == "private, no-store"
        assert preview.json()["preview_image_url"].endswith("/preview/image")
        preview_image = await client.get(preview.json()["preview_image_url"])
        assert preview_image.status_code == 200
        assert preview_image.headers["content-type"] == "image/png"
        assert preview_image.content == thumbnail.content

        other_subject = await client.get(
            f"/api/subjects/{other_subject_seeded.subject.id}/editor/figures/"
            f"{item['figure_id']}/thumbnail"
        )
        assert other_subject.status_code == 404, other_subject.json()
        assert other_subject.json()["detail"]["code"] == "editorial_figure_not_found"
        assert _PRIVATE_TEXT not in other_subject.text

        raw_blob_key = await client.get(f"{path}/{blob_ids[1]}/thumbnail")
        assert raw_blob_key.status_code == 404, raw_blob_key.json()
        assert raw_blob_key.json()["detail"]["code"] == "editorial_figure_not_found"

        async with uow_factory() as uow:
            extraction_artifact = await uow.production_artifacts.get_current(
                seeded.run.id, ProductionArtifactStage.EXTRACTION.value
            )
            assert extraction_artifact is not None
            newer_run = ProductionRun(
                subject_id=seeded.subject.id,
                edition_id=seeded.subject.edition_id,
                run_number=seeded.run.run_number + 1,
                status=ProductionRunStatus.READY,
            )
            newer_input = replace(
                seeded.production_input,
                production_run_id=newer_run.id,
                id=uuid4(),
            )
            newer_extraction = replace(
                extraction_artifact,
                id=uuid4(),
                production_run_id=newer_run.id,
                version=1,
            )
            await uow.production_runs.add(newer_run)
            await uow.production_input_snapshots.add(newer_input)
            await uow.production_artifacts.append(newer_extraction)
            await uow.commit()
        prior_run = await client.get(f"{path}/{item['figure_id']}/thumbnail")
        assert prior_run.status_code == 404, prior_run.json()
        assert prior_run.json()["detail"]["code"] == "editorial_figure_not_found"

    server_logs = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("cti_app")
    )
    assert _PRIVATE_TEXT not in server_logs
    assert f"q={search_query}" not in server_logs


@pytest.mark.asyncio
async def test_warm_catalog_rejects_figure_after_current_tlp_rises(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=0, seed_discovery_lineage=True)
    _source_id, _blob_ids, _object_keys, collection_id = await _archive_figure_sources(
        seeded,
        uow_factory,
        source_tlp=TLP.AMBER,
        with_collection=True,
    )
    assert collection_id is not None
    app = _app(seeded, uow_factory, monkeypatch)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        path = f"/api/subjects/{seeded.subject.id}/editor/figures"
        catalogue = await client.get(path)
        assert catalogue.status_code == 200, catalogue.json()
        figure_id = catalogue.json()["items"][0]["figure_id"]

        async with uow_factory() as uow:
            subject = await uow.subjects.get(seeded.subject.id)
            assert subject is not None
            expected_version = subject.version
            subject.update_metadata(title=subject.title, tlp=TLP.AMBER_STRICT)
            assert await uow.subjects.update(subject, expected_version=expected_version)
            await uow.commit()

        blocked = await client.get(f"{path}/{figure_id}/thumbnail")

    assert blocked.status_code == 404, blocked.json()
    assert blocked.json()["detail"]["code"] == "editorial_figure_not_found"
    assert _PRIVATE_TEXT not in blocked.text


@pytest.mark.asyncio
async def test_warm_catalog_rejects_figure_after_collection_revocation(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=0, seed_discovery_lineage=True)
    _source_id, _blob_ids, _object_keys, collection_id = await _archive_figure_sources(
        seeded,
        uow_factory,
        source_tlp=TLP.AMBER,
        with_collection=True,
    )
    assert collection_id is not None
    app = _app(seeded, uow_factory, monkeypatch)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        path = f"/api/subjects/{seeded.subject.id}/editor/figures"
        catalogue = await client.get(path)
        assert catalogue.status_code == 200, catalogue.json()
        figure_id = catalogue.json()["items"][0]["figure_id"]

        async with uow_factory() as uow:
            collection = await uow.source_collections.get(collection_id)
            assert collection is not None
            collection.state = CollectionState.UNAVAILABLE
            await uow.source_collections.save(collection)
            await uow.commit()

        blocked = await client.get(f"{path}/{figure_id}/thumbnail")

    assert blocked.status_code == 404, blocked.json()
    assert blocked.json()["detail"]["code"] == "editorial_figure_not_found"
    assert _PRIVATE_TEXT not in blocked.text

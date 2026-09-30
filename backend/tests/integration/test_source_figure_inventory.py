from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import SourceDocument, Subject
from cti_app.domain.production_extraction import ProductionSourceExtractionV1
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore

from .edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_inventory_resolves_only_an_image_blob_already_in_the_archive(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition = Edition(
        country="Source figure inventory",
        country_code=reserve_edition_code(),
        period_start=datetime(2026, 9, 1).date(),
        period_end=datetime(2026, 9, 30).date(),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Archived figure test",
        slug=f"source-figure-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    html_url = "https://publisher.test/report.html"
    image_url = "https://publisher.test/figures/map.png"
    png = b"\x89PNG\r\n\x1a\narchived-map"
    catalog = BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    html_blob = await catalog.ingest(
        BytesIO(f'<img src="{image_url}" alt="Network map">'.encode()),
        logical_bucket="source-documents",
        mime_type="text/html",
    )
    image_blob = await catalog.ingest(
        BytesIO(png), logical_bucket="source-documents", mime_type="image/png"
    )
    acquired_at = datetime(2026, 9, 30, tzinfo=UTC)
    html_document = SourceDocument(
        subject_id=subject.id,
        blob_id=html_blob.id,
        original_name="report.html",
        origin=html_url,
        acquired_at=acquired_at,
        license_restriction=None,
        tlp=TLP.AMBER,
        do_not_submit=False,
        external_llm_allowed=True,
        final_url=html_url,
        detected_mime_type="text/html",
    )
    image_document = SourceDocument(
        subject_id=subject.id,
        blob_id=image_blob.id,
        original_name="map.png",
        origin=image_url,
        acquired_at=acquired_at,
        license_restriction=None,
        tlp=TLP.AMBER,
        do_not_submit=False,
        external_llm_allowed=True,
        final_url=image_url,
        detected_mime_type="image/png",
    )
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.source_documents.add(html_document)
        await uow.source_documents.add(image_document)
        await uow.commit()

    extraction_source = cast(
        ProductionSourceExtractionV1,
        SimpleNamespace(source_document_id=html_document.id, canonical_url=html_url),
    )
    async with uow_factory() as uow:
        inventory = await load_archived_source_figure_inventory(
            subject_id=subject.id,
            extraction_sources=(extraction_source,),
            source_document_repository=uow.source_documents,
            blob_repository=uow.blobs,
            artifact_store=ProductionArtifactStore(catalog),
        )

    assert len(inventory.accepted) == 1
    figure = inventory.accepted[0]
    assert figure.blob_id == image_blob.id
    assert figure.source_document_id == html_document.id
    assert figure.sha256 == image_blob.descriptor.sha256
    assert figure.mime_type == "image/png"

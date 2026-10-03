from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.diagram_compilation import CompiledDiagram
from cti_app.application.media_assets import MediaAssetStore, SourceFigureIngestor
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.media_assets import MediaAssetKind, media_asset_id
from cti_app.domain.production_editorial_enrichment import (
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_media_asset_store_deduplicates_and_persists_complete_manifests(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    catalog = BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    blob_store = ProductionArtifactStore(catalog)
    asset_store = MediaAssetStore(blob_store, uow_factory)
    content = b"\x89PNG\r\n\x1a\n" + uuid4().bytes
    document_id = uuid4()
    source = "https://publisher.test/report.html"
    locator = SourceFigureLocatorV1(page=3, figure_label="Network map")
    figure = ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=document_id,
            sha256=hashlib.sha256(content).hexdigest(),
            source=source,
            locator=locator,
        ),
        blob_id=uuid4(),
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/png",
        byte_size=len(content),
        source_document_id=document_id,
        source=source,
        provenance="Archived image asset",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="matched_archived_blob",
    )

    first = await SourceFigureIngestor(asset_store).ingest(figure, content)
    repeated = await SourceFigureIngestor(asset_store).ingest(figure, content)

    assert first.asset_id == repeated.asset_id
    assert first.blob_id == repeated.blob_id
    assert first.asset_id == media_asset_id(hashlib.sha256(content).hexdigest(), "image/png")
    assert first.kind is MediaAssetKind.SOURCE_FIGURE
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    assert first.mime_type == "image/png"
    assert first.byte_size == len(content)
    assert first.source == source
    assert first.policy_version == "source-figure-ingestion-v1"
    assert first.provenance == "Archived image asset"
    assert first.locator == {
        "page": 3,
        "section": None,
        "figure_label": "Network map",
        "original_asset_url": None,
    }
    assert first.decision == "accepted"
    assert await asset_store.get(first.asset_id) == first
    assert await asset_store.read(first.asset_id) == content

    svg = f"<svg xmlns='http://www.w3.org/2000/svg'><text>{uuid4()}</text></svg>".encode()
    compiled = CompiledDiagram(
        diagram_key="network_flow",
        source_format="d2",
        source_bytes=b"network_flow: source",
        source_sha256=hashlib.sha256(b"network_flow: source").hexdigest(),
        media_type="image/svg+xml",
        media_bytes=svg,
        media_sha256=hashlib.sha256(svg).hexdigest(),
        compiler="d2",
        compiler_version="0.9.0",
        compiler_policy_version="diagram-d2-svg-v3-relation-semantics",
    )
    diagram_manifest = await asset_store.store_diagram(
        compiled, source=f"production_run:{uuid4()}:diagram:network_flow"
    )

    assert diagram_manifest.kind is MediaAssetKind.DIAGRAM_SVG
    assert diagram_manifest.asset_id == media_asset_id(
        hashlib.sha256(svg).hexdigest(), "image/svg+xml"
    )
    assert diagram_manifest.compiler_name == "d2"
    assert diagram_manifest.compiler_version == "0.9.0"
    assert diagram_manifest.policy_version == "diagram-d2-svg-v3-relation-semantics"
    assert diagram_manifest.source.startswith("production_run:")
    assert diagram_manifest.provenance is None
    assert diagram_manifest.locator is None
    assert diagram_manifest.decision is None
    assert await asset_store.read(diagram_manifest.asset_id) == svg

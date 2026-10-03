from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.diagram_compilation import CompiledDiagram
from cti_app.application.media_assets import (
    MAX_SOURCE_FIGURE_BYTES,
    MediaAssetStore,
    SourceFigureIngestor,
    compile_and_store_diagrams,
)
from cti_app.domain.media_assets import MediaAssetKind, MediaAssetManifest, media_asset_id
from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramGroupV1,
    DiagramNodeV1,
    DiagramSpecV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1

_PNG = b"\x89PNG\r\n\x1a\n" + uuid4().bytes


class _MemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[UUID, bytes] = {}
        self.ids_by_address: dict[tuple[str, str], UUID] = {}
        self.put_calls: list[tuple[str, str]] = []

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        digest = hashlib.sha256(content).hexdigest()
        address = bucket, digest
        self.put_calls.append(address)
        blob_id = self.ids_by_address.setdefault(address, uuid4())
        self.objects[blob_id] = content
        return blob_id

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self.objects[blob_id]
        if len(content) > max_bytes:
            raise ValueError("blob exceeds read limit")
        return content


class _MemoryMediaAssets:
    def __init__(self) -> None:
        self.by_id: dict[UUID, MediaAssetManifest] = {}

    async def add_if_absent(self, asset: MediaAssetManifest) -> MediaAssetManifest:
        return self.by_id.setdefault(asset.asset_id, asset)

    async def get(self, asset_id: UUID) -> MediaAssetManifest | None:
        return self.by_id.get(asset_id)

    async def get_by_identity(self, sha256: str, mime_type: str) -> MediaAssetManifest | None:
        return next(
            (
                asset
                for asset in self.by_id.values()
                if asset.sha256 == sha256 and asset.mime_type == mime_type
            ),
            None,
        )


class _MemoryUow:
    def __init__(self, media_assets: _MemoryMediaAssets) -> None:
        self.media_assets = media_assets

    async def __aenter__(self) -> _MemoryUow:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _DiagramCompiler:
    def __init__(self, svg: bytes) -> None:
        self.svg = svg
        self.seen: list[DiagramSpecV1] = []

    async def compile(self, diagram: DiagramSpecV1) -> CompiledDiagram:
        self.seen.append(diagram)
        return CompiledDiagram(
            diagram_key=diagram.key,
            source_format="d2",
            source_bytes=b"diagram source",
            source_sha256=hashlib.sha256(b"diagram source").hexdigest(),
            media_type="image/svg+xml",
            media_bytes=self.svg,
            media_sha256=hashlib.sha256(self.svg).hexdigest(),
            compiler="d2",
            compiler_version="0.9.0",
            compiler_policy_version="diagram-d2-svg-v3-relation-semantics",
        )


def _figure(
    *,
    content: bytes = _PNG,
    mime_type: str = "image/png",
    decision: SourceFigureDecision = SourceFigureDecision.ACCEPTED,
    byte_size: int | None = None,
    sha256: str | None = None,
) -> ResolvedSourceFigureV1:
    source_document_id = UUID(int=101)
    source = "https://publisher.test/report.html"
    locator = SourceFigureLocatorV1(page=1, figure_label="Network map")
    digest = sha256 or hashlib.sha256(content).hexdigest()
    payload = dict(
        figure_id=source_figure_id(
            source_document_id=source_document_id,
            sha256=digest,
            source=source,
            locator=locator,
        ),
        blob_id=UUID(int=202),
        sha256=digest,
        mime_type=mime_type,  # type: ignore[arg-type]
        byte_size=len(content) if byte_size is None else byte_size,
        source_document_id=source_document_id,
        source=source,
        provenance="Archived image asset",
        locator=locator,
        decision=decision,
        decision_reason="matched_archived_blob",
    )
    if mime_type not in {"image/png", "image/jpeg", "image/svg+xml", "image/webp", "image/gif"}:
        return ResolvedSourceFigureV1.model_construct(**payload)
    return ResolvedSourceFigureV1(**payload)


def _diagram() -> DiagramSpecV1:
    evidence = (ExtractionEvidenceRefV1(UUID(int=1), EvidenceKind.FACT, "a" * 64),)
    return DiagramSpecV1(
        key="network_flow",
        kind=EnrichmentDiagramKind.NETWORK_FLOW,
        title="Network flow",
        caption=None,
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("source", "Source", evidence),
            DiagramNodeV1("target", "Target", evidence),
        ),
        edges=(DiagramEdgeV1("source", "target", None, evidence),),
        groups=(DiagramGroupV1("group", "Group", ("source", "target")),),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
    )


@pytest.mark.asyncio
async def test_media_asset_store_is_idempotent_and_reads_by_asset_id() -> None:
    blob_store = _MemoryBlobStore()
    media_assets = _MemoryMediaAssets()
    asset_store = MediaAssetStore(blob_store, lambda: _MemoryUow(media_assets))  # type: ignore[arg-type]

    first = await asset_store.put(
        _PNG,
        kind=MediaAssetKind.SOURCE_FIGURE,
        mime_type="image/png",
        source="https://publisher.test/report.html",
        policy_version="source-figure-ingestion-v1",
        provenance="Archived image asset",
        locator={"page": 1, "figure_label": "Network map"},
        decision="accepted",
    )
    repeated = await asset_store.put(
        _PNG,
        kind=MediaAssetKind.SOURCE_FIGURE,
        mime_type="image/png",
        source="https://publisher.test/report.html",
        policy_version="source-figure-ingestion-v1",
        provenance="Archived image asset",
        locator={"page": 1, "figure_label": "Network map"},
        decision="accepted",
    )
    same_bytes_other_mime = await asset_store.put(
        _PNG,
        kind=MediaAssetKind.SOURCE_FIGURE,
        mime_type="image/jpeg",
        source="https://publisher.test/other-report.html",
        policy_version="source-figure-ingestion-v1",
        provenance="Archived image asset",
        locator={"page": 1, "figure_label": "Other label"},
        decision="accepted",
    )

    assert first == repeated
    assert first.asset_id == media_asset_id(hashlib.sha256(_PNG).hexdigest(), "image/png")
    assert first.sha256 == hashlib.sha256(_PNG).hexdigest()
    assert first.byte_size == len(_PNG)
    assert first.provenance == "Archived image asset"
    assert first.locator == {"page": 1, "figure_label": "Network map"}
    assert first.decision == "accepted"
    assert same_bytes_other_mime.sha256 == first.sha256
    assert same_bytes_other_mime.asset_id != first.asset_id
    assert same_bytes_other_mime.blob_id != first.blob_id
    assert len(media_assets.by_id) == 2
    assert len(blob_store.put_calls) == 3
    assert await asset_store.get(first.asset_id) == first
    assert await asset_store.read(first.asset_id) == _PNG


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("figure", "content", "message"),
    [
        (_figure(mime_type="image/bmp"), _PNG, "MIME type"),
        (_figure(byte_size=len(_PNG) + 1), _PNG, "byte size"),
        (_figure(sha256="f" * 64), _PNG, "SHA-256"),
        (_figure(content=b"X" * len(_PNG)), b"X" * len(_PNG), "MIME type"),
        (_figure(decision=SourceFigureDecision.PENDING), _PNG, "Only accepted"),
    ],
)
async def test_source_figure_ingestor_rejects_invalid_inputs(
    figure: ResolvedSourceFigureV1, content: bytes, message: str
) -> None:
    class Store:
        called = False

        async def put(self, *_: Any, **__: Any) -> None:
            self.called = True

    store = Store()
    with pytest.raises(ValueError, match=message):
        await SourceFigureIngestor(store).ingest(figure, content)  # type: ignore[arg-type]
    assert not store.called


@pytest.mark.asyncio
async def test_source_figure_ingestor_persists_full_manifest_metadata() -> None:
    class Store:
        kwargs: dict[str, Any] | None = None

        async def put(self, content: bytes, **kwargs: Any) -> MediaAssetManifest:
            self.kwargs = kwargs
            return MediaAssetManifest(
                asset_id=media_asset_id(hashlib.sha256(content).hexdigest(), kwargs["mime_type"]),
                kind=kwargs["kind"],
                blob_id=UUID(int=303),
                sha256=hashlib.sha256(content).hexdigest(),
                mime_type=kwargs["mime_type"],
                byte_size=len(content),
                source=kwargs["source"],
                policy_version=kwargs["policy_version"],
                provenance=kwargs["provenance"],
                locator=kwargs["locator"],
                decision=kwargs["decision"],
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )

    store = Store()
    manifest = await SourceFigureIngestor(store).ingest(_figure(), _PNG)  # type: ignore[arg-type]

    assert store.kwargs is not None
    assert manifest.kind is MediaAssetKind.SOURCE_FIGURE
    assert manifest.mime_type == "image/png"
    assert manifest.byte_size == len(_PNG)
    assert manifest.sha256 == hashlib.sha256(_PNG).hexdigest()
    assert manifest.source == "https://publisher.test/report.html"
    assert manifest.provenance == "Archived image asset"
    assert manifest.locator == {
        "page": 1,
        "section": None,
        "figure_label": "Network map",
        "original_asset_url": None,
    }
    assert manifest.decision == "accepted"


@pytest.mark.asyncio
async def test_source_figure_ingestor_rejects_oversized_content() -> None:
    class Store:
        async def put(self, *_: Any, **__: Any) -> None:
            pytest.fail("Oversized input must be rejected before persistence")

    content = b"\x89PNG\r\n\x1a\n" + b"x" * MAX_SOURCE_FIGURE_BYTES
    with pytest.raises(ValueError, match="byte limit"):
        await SourceFigureIngestor(Store()).ingest(_figure(content=content), content)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_diagram_compilation_persists_svg_and_exposes_compiled_asset_id() -> None:
    blob_store = _MemoryBlobStore()
    media_assets = _MemoryMediaAssets()
    asset_store = MediaAssetStore(blob_store, lambda: _MemoryUow(media_assets))  # type: ignore[arg-type]
    compiler = _DiagramCompiler(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    run_id = uuid4()

    diagrams = await compile_and_store_diagrams(
        (_diagram(),),
        compiler=compiler,
        media_asset_store=asset_store,
        production_run_id=run_id,
    )

    assert len(compiler.seen) == 1
    assert diagrams[0].compiled_asset_id == media_asset_id(
        hashlib.sha256(compiler.svg).hexdigest(), "image/svg+xml"
    )
    manifest = await asset_store.get(diagrams[0].compiled_asset_id)
    assert manifest is not None
    assert manifest.kind is MediaAssetKind.DIAGRAM_SVG
    assert manifest.compiler_name == "d2"
    assert manifest.compiler_version == "0.9.0"
    assert manifest.policy_version == "diagram-d2-svg-v3-relation-semantics"
    assert manifest.source == f"production_run:{run_id}:diagram:network_flow"
    assert await asset_store.read(manifest.asset_id) == compiler.svg

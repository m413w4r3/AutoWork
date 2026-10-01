from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application import publication_rendering
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import PublicationRenderUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    TypstCompilationError,
    TypstCompileFailedError,
    TypstCompileRequest,
    TypstCompilerUnavailableError,
    TypstCompilerVersionMismatchError,
    TypstCompileTimeoutError,
    TypstOutputInvalidError,
    TypstOutputTooLargeError,
)
from cti_app.application.typst_rendering import TypstMediaRef, TypstRenderer, TypstRenderSource
from cti_app.domain.errors import BlobIntegrityError
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    publication_document_v4_to_json,
)
from cti_app.domain.publication_render import (
    PublicationRender,
    PublicationRenderStatus,
)


def _document() -> PublicationDocumentV4:
    return PublicationDocumentV4(
        schema_version="4",
        subject_id=uuid4(),
        publication_language="fr",
        title="Service fixture",
        lead=(),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
    )


class FakeProductionArtifacts:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.artifacts = artifacts

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.artifacts.get(artifact_id)


class FakePublicationRenders:
    def __init__(self) -> None:
        self.renders: dict[UUID, PublicationRender] = {}

    async def get_by_input_hash(self, input_hash: str) -> PublicationRender | None:
        return next(
            (render for render in self.renders.values() if render.input_hash == input_hash),
            None,
        )

    async def add(self, render: PublicationRender) -> PublicationRender:
        if await self.get_by_input_hash(render.input_hash) is not None:
            raise ValueError("duplicate_publication_render_input_hash")
        self.renders[render.id] = render
        return render

    async def mark_retrying(self, render_id: UUID) -> PublicationRender:
        render = self.renders[render_id]
        updated = replace(
            render,
            status=PublicationRenderStatus.RUNNING,
            error_code=None,
            error_message=None,
            updated_at=datetime.now(UTC),
        )
        self.renders[render_id] = updated
        return updated

    async def mark_succeeded(
        self,
        render_id: UUID,
        *,
        output_blob_id: UUID,
        output_sha256: str,
        output_byte_size: int,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> PublicationRender:
        render = self.renders[render_id]
        if render.status is not PublicationRenderStatus.RUNNING:
            raise ValueError("publication_render_not_running")
        updated = replace(
            render,
            source_blob_id=source_blob_id,
            render_data_blob_id=render_data_blob_id,
            output_blob_id=output_blob_id,
            output_sha256=output_sha256,
            output_byte_size=output_byte_size,
            status=PublicationRenderStatus.SUCCEEDED,
            error_code=None,
            error_message=None,
            updated_at=datetime.now(UTC),
        )
        self.renders[render_id] = updated
        return updated

    async def mark_failed(
        self,
        render_id: UUID,
        *,
        error_code: str,
        error_message: str | None = None,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> PublicationRender:
        render = self.renders[render_id]
        if render.status is not PublicationRenderStatus.RUNNING:
            raise ValueError("publication_render_not_running")
        updated = replace(
            render,
            source_blob_id=source_blob_id,
            render_data_blob_id=render_data_blob_id,
            status=PublicationRenderStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            updated_at=datetime.now(UTC),
        )
        self.renders[render_id] = updated
        return updated


class FakeUnitOfWork:
    def __init__(
        self,
        production_artifacts: FakeProductionArtifacts,
        publication_renders: FakePublicationRenders,
    ) -> None:
        self.production_artifacts = production_artifacts
        self.publication_renders = publication_renders

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class FakeUnitOfWorkFactory:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.production_artifacts = FakeProductionArtifacts(artifacts)
        self.publication_renders = FakePublicationRenders()

    def __call__(self) -> FakeUnitOfWork:
        return FakeUnitOfWork(self.production_artifacts, self.publication_renders)


class FakeArtifactStore:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payloads: dict[UUID, dict[str, Any]] = {}
        self.blobs: dict[UUID, bytes] = {}
        self.writes: list[tuple[str, str, UUID, bytes]] = []
        self.read_json_error: Exception | None = None
        self.default_payload = payload

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        if self.read_json_error is not None:
            raise self.read_json_error
        if blob_id in self.payloads:
            return self.payloads[blob_id]
        assert self.default_payload is not None
        return self.default_payload

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        del max_bytes
        return self.blobs[blob_id]

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        blob_id = uuid4()
        self.blobs[blob_id] = content
        self.writes.append((bucket, mime_type, blob_id, content))
        return blob_id


class FakeMediaAssets:
    def __init__(self) -> None:
        self.manifests: dict[UUID, object] = {}
        self.content: dict[UUID, bytes] = {}
        self.read_error: Exception | None = None

    async def get(self, asset_id: UUID) -> object | None:
        return self.manifests.get(asset_id)

    async def read(self, asset_id: UUID) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.content[asset_id]


class FakeRenderer:
    def __init__(self, media_refs: tuple[TypstMediaRef, ...] = ()) -> None:
        self.media_refs = media_refs
        self.call_count = 0

    def render(self, document: PublicationDocumentV4) -> TypstRenderSource:
        del document
        self.call_count += 1
        source_bytes = b'#let publication = json("render-data.json")\n'
        render_data = b'{"schema_version":"typst-publication-model-v1"}'
        return TypstRenderSource(
            source_bytes=source_bytes,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            render_data_bytes=render_data,
            render_data_sha256=hashlib.sha256(render_data).hexdigest(),
            media_refs=self.media_refs,
        )


class FakeCompiler:
    def __init__(self, failures: tuple[Exception, ...] = ()) -> None:
        self.failures = list(failures)
        self.call_count = 0
        self.requests: list[TypstCompileRequest] = []
        self.workspace_snapshots: list[dict[str, bytes]] = []
        self.content = b"%PDF-canned-publication"

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.call_count += 1
        self.requests.append(request)
        self.workspace_snapshots.append(
            {
                str(path.relative_to(request.workspace_root)): path.read_bytes()
                for path in request.workspace_root.rglob("*")
                if path.is_file()
            }
        )
        if self.failures:
            raise self.failures.pop(0)
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=self.content,
            sha256=hashlib.sha256(self.content).hexdigest(),
            byte_size=len(self.content),
            compiler="typst",
            compiler_version="0.15.1",
        )


def _media_ref(
    *,
    kind: MediaAssetKind,
    asset_id: UUID | None = None,
    sha256: str | None = None,
    mime_type: str = "image/svg+xml",
    byte_size: int | None = None,
) -> TypstMediaRef:
    identifier = asset_id or uuid4()
    extension = ".svg" if mime_type == "image/svg+xml" else ".png"
    return TypstMediaRef(
        asset_id=identifier,
        expected_kind=kind,
        expected_mime_type=mime_type,
        expected_sha256=sha256,
        expected_byte_size=byte_size,
        media_path=f"media/{identifier}{extension}",
    )


def _make_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    media_refs: tuple[TypstMediaRef, ...] = (),
    compiler: FakeCompiler | None = None,
    valid_document: bool = True,
    artifact_status: ProductionArtifactStatus = ProductionArtifactStatus.VERIFIED,
    artifact_stage: ProductionArtifactStage = ProductionArtifactStage.PUBLICATION,
    canonical_blob: UUID | None = None,
    missing_canonical: bool = False,
) -> tuple[
    publication_rendering.PublicationRenderService,
    ProductionArtifact,
    FakeUnitOfWorkFactory,
    FakeArtifactStore,
    FakeMediaAssets,
    FakeCompiler,
]:
    document = _document()
    payload = (
        publication_document_v4_to_json(document) if valid_document else {"schema_version": "bad"}
    )
    artifact_store = FakeArtifactStore(payload)
    canonical_blob_id = None if missing_canonical else (canonical_blob or uuid4())
    artifact = ProductionArtifact(
        production_run_id=uuid4(),
        subject_id=document.subject_id,
        stage=artifact_stage,
        version=1,
        input_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        status=artifact_status,
        canonical_blob_id=canonical_blob_id,
    )
    uow_factory = FakeUnitOfWorkFactory({artifact.id: artifact})
    media_assets = FakeMediaAssets()
    renderer = FakeRenderer(media_refs)
    fake_compiler = compiler or FakeCompiler()
    template_root = tmp_path / "chpTypst"
    for relative, content in (
        ("RENDERER/publication.typ", b'#let publication = json("render-data.json")\n'),
        ("UTILS/helpers.typ", b"// renderer helper\n"),
    ):
        path = template_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (template_root / "TEMPLATES").mkdir()
    (template_root / "TEMPLATES" / "reference.typ").write_bytes(b"// not copied\n")
    (template_root / "renderer-manifest.json").write_text(
        json.dumps(
            {
                "template_version": "unit-template-v1",
                "files": ["RENDERER/publication.typ", "UTILS/helpers.typ"],
            }
        ),
        encoding="utf-8",
    )
    lock_path = tmp_path / "typst-fonts.lock"
    lock_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        publication_rendering,
        "compute_template_bundle_hash",
        lambda _: ("unit-template-v1", "a" * 64),
    )
    monkeypatch.setattr(
        publication_rendering,
        "compute_font_bundle_version",
        lambda *_: "unit-fonts-v1",
    )
    service = publication_rendering.PublicationRenderService(
        uow_factory=cast(PublicationRenderUnitOfWorkFactory, uow_factory),
        artifact_store=cast(ProductionArtifactStore, artifact_store),
        media_asset_store=cast(MediaAssetStore, media_assets),
        renderer=cast(TypstRenderer, renderer),
        compiler=fake_compiler,
        chp_typst_root=template_root,
        font_paths=(),
        typst_fonts_lock_path=lock_path,
    )
    return service, artifact, uow_factory, artifact_store, media_assets, fake_compiler


@pytest.mark.asyncio
async def test_publication_render_service_happy_path_persists_content_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, uow_factory, store, _, compiler = _make_service(tmp_path, monkeypatch)

    render = await service.render_pdf(artifact.id)

    assert render.status is PublicationRenderStatus.SUCCEEDED
    assert render.publication_artifact_id == artifact.id
    assert render.source_blob_id is not None
    assert render.render_data_blob_id is not None
    assert render.output_blob_id is not None
    assert render.output_sha256 == hashlib.sha256(compiler.content).hexdigest()
    assert render.output_byte_size == len(compiler.content)
    assert store.blobs[render.source_blob_id] == b'#let publication = json("render-data.json")\n'
    assert set(compiler.workspace_snapshots[0]) == {
        "RENDERER/publication.typ",
        "RENDERER/render-data.json",
        "UTILS/helpers.typ",
    }
    assert [write[0] for write in store.writes] == [
        "publication-renders-source",
        "publication-renders-render-data",
        "publication-renders-output",
    ]
    assert compiler.call_count == 1
    assert uow_factory.publication_renders.renders[render.id] == render


@pytest.mark.asyncio
async def test_publication_render_service_reuses_succeeded_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, _, _, compiler = _make_service(tmp_path, monkeypatch)

    first = await service.render_pdf(artifact.id)
    second = await service.render_pdf(artifact.id)

    assert second == first
    assert compiler.call_count == 1


@pytest.mark.asyncio
async def test_publication_render_service_creates_new_row_when_template_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, _, _, compiler = _make_service(tmp_path, monkeypatch)
    template_hashes = iter((("template-v1", "a" * 64), ("template-v2", "b" * 64)))
    monkeypatch.setattr(
        publication_rendering,
        "compute_template_bundle_hash",
        lambda _: next(template_hashes),
    )

    first = await service.render_pdf(artifact.id)
    second = await service.render_pdf(artifact.id)

    assert first.id != second.id
    assert first.input_hash != second.input_hash
    assert first.publication_artifact_id == second.publication_artifact_id == artifact.id
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_publication_render_service_retries_failed_input_using_same_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = TypstCompileFailedError("initial compile failed")
    compiler = FakeCompiler((failure,))
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path, monkeypatch, compiler=compiler
    )

    with pytest.raises(TypstCompileFailedError) as raised:
        await service.render_pdf(artifact.id)
    failed = next(iter(uow_factory.publication_renders.renders.values()))
    retried = await service.render_pdf(artifact.id)

    assert raised.value is failure
    assert failed.status is PublicationRenderStatus.FAILED
    assert failed.error_code == "typst_compile_failed"
    assert retried.id == failed.id
    assert retried.status is PublicationRenderStatus.SUCCEEDED
    assert retried.error_code is None
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_publication_render_service_rejects_missing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _artifact, uow_factory, _, _, compiler = _make_service(tmp_path, monkeypatch)

    with pytest.raises(publication_rendering.PublicationRenderArtifactMissingError) as raised:
        await service.render_pdf(uuid4())

    assert raised.value.code == "publication_render_artifact_missing"
    assert not uow_factory.publication_renders.renders
    assert compiler.call_count == 0


@pytest.mark.parametrize(
    ("artifact_stage", "artifact_status", "missing_canonical"),
    (
        (ProductionArtifactStage.EXTRACTION, ProductionArtifactStatus.VERIFIED, False),
        (ProductionArtifactStage.PUBLICATION, ProductionArtifactStatus.STALE, False),
        (ProductionArtifactStage.PUBLICATION, ProductionArtifactStatus.VERIFIED, True),
    ),
)
@pytest.mark.asyncio
async def test_publication_render_service_rejects_invalid_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_stage: ProductionArtifactStage,
    artifact_status: ProductionArtifactStatus,
    missing_canonical: bool,
) -> None:
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path,
        monkeypatch,
        artifact_stage=artifact_stage,
        artifact_status=artifact_status,
        missing_canonical=missing_canonical,
    )

    with pytest.raises(publication_rendering.PublicationRenderArtifactInvalidError) as raised:
        await service.render_pdf(artifact.id)

    assert raised.value.code == "publication_render_artifact_invalid"
    assert not uow_factory.publication_renders.renders
    assert compiler.call_count == 0


@pytest.mark.asyncio
async def test_publication_render_service_maps_document_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path, monkeypatch, valid_document=False
    )

    with pytest.raises(publication_rendering.PublicationRenderDocumentInvalidError) as raised:
        await service.render_pdf(artifact.id)

    assert raised.value.code == "publication_render_document_invalid"
    assert not uow_factory.publication_renders.renders
    assert compiler.call_count == 0


@pytest.mark.asyncio
async def test_publication_render_service_marks_missing_media_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(kind=MediaAssetKind.DIAGRAM_SVG)
    service, artifact, uow_factory, _, _, _ = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )

    with pytest.raises(publication_rendering.PublicationRenderMediaMissingError) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value.code == "publication_render_media_missing"
    assert render.status is PublicationRenderStatus.FAILED
    assert render.error_code == "publication_render_media_missing"


@pytest.mark.asyncio
async def test_publication_render_service_marks_diagram_kind_mismatch_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(kind=MediaAssetKind.DIAGRAM_SVG)
    service, artifact, uow_factory, _, media_assets, _ = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )
    media_assets.manifests[ref.asset_id] = SimpleNamespace(
        asset_id=ref.asset_id,
        kind=MediaAssetKind.SOURCE_FIGURE,
        sha256="a" * 64,
        mime_type="image/png",
        byte_size=8,
    )

    with pytest.raises(publication_rendering.PublicationRenderMediaKindMismatchError) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value.code == "publication_render_media_kind_mismatch"
    assert render.status is PublicationRenderStatus.FAILED
    assert render.error_code == "publication_render_media_kind_mismatch"


@pytest.mark.asyncio
async def test_publication_render_service_writes_resolved_media_to_synthetic_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(kind=MediaAssetKind.DIAGRAM_SVG)
    service, artifact, _, _, media_assets, compiler = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )
    content = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    media_assets.manifests[ref.asset_id] = SimpleNamespace(
        asset_id=ref.asset_id,
        kind=MediaAssetKind.DIAGRAM_SVG,
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/svg+xml",
        byte_size=len(content),
    )
    media_assets.content[ref.asset_id] = content

    render = await service.render_pdf(artifact.id)

    assert render.status is PublicationRenderStatus.SUCCEEDED
    assert compiler.workspace_snapshots[0][f"RENDERER/{ref.media_path}"] == content


@pytest.mark.asyncio
async def test_publication_render_service_recompiles_if_cached_output_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, store, _, compiler = _make_service(tmp_path, monkeypatch)

    first = await service.render_pdf(artifact.id)
    assert first.output_blob_id is not None
    del store.blobs[first.output_blob_id]
    second = await service.render_pdf(artifact.id)

    assert second.id == first.id
    assert second.status is PublicationRenderStatus.SUCCEEDED
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_publication_render_service_marks_figure_integrity_mismatch_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(
        kind=MediaAssetKind.SOURCE_FIGURE,
        sha256="a" * 64,
        mime_type="image/png",
        byte_size=12,
    )
    service, artifact, uow_factory, _, media_assets, _ = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )
    media_assets.manifests[ref.asset_id] = SimpleNamespace(
        asset_id=ref.asset_id,
        kind=MediaAssetKind.SOURCE_FIGURE,
        sha256="b" * 64,
        mime_type="image/jpeg",
        byte_size=13,
    )

    with pytest.raises(
        publication_rendering.PublicationRenderMediaIntegrityMismatchError
    ) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value.code == "publication_render_media_integrity_mismatch"
    assert render.status is PublicationRenderStatus.FAILED
    assert render.error_code == "publication_render_media_integrity_mismatch"


@pytest.mark.asyncio
async def test_publication_render_service_maps_media_blob_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(kind=MediaAssetKind.DIAGRAM_SVG)
    service, artifact, uow_factory, _, media_assets, _ = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )
    media_assets.manifests[ref.asset_id] = SimpleNamespace(
        asset_id=ref.asset_id,
        kind=MediaAssetKind.DIAGRAM_SVG,
        sha256="a" * 64,
        mime_type="image/svg+xml",
        byte_size=8,
    )
    media_assets.read_error = BlobIntegrityError("digest mismatch")

    with pytest.raises(
        publication_rendering.PublicationRenderMediaIntegrityMismatchError
    ) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value.code == "publication_render_media_integrity_mismatch"
    assert render.status is PublicationRenderStatus.FAILED


@pytest.mark.parametrize(
    "error_type",
    (
        TypstCompilerUnavailableError,
        TypstCompilerVersionMismatchError,
        TypstCompileTimeoutError,
        TypstCompileFailedError,
        TypstOutputTooLargeError,
        TypstOutputInvalidError,
    ),
)
@pytest.mark.asyncio
async def test_publication_render_service_persists_typed_compiler_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[TypstCompilationError],
) -> None:
    failure = error_type("compiler failure")
    compiler = FakeCompiler((failure,))
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path, monkeypatch, compiler=compiler
    )

    with pytest.raises(error_type) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value is failure
    assert render.status is PublicationRenderStatus.FAILED
    assert render.error_code == failure.code
    assert render.source_blob_id is not None
    assert render.render_data_blob_id is not None
    assert compiler.call_count == 1

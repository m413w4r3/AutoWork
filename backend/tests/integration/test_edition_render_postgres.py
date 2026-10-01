import asyncio
import calendar
import hashlib
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.edition_rendering import EditionRenderService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    TypstCompileFailedError,
    TypstCompileRequest,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionPublicationV2,
    EditionRelease,
    PublicationManifestV1,
)
from cti_app.domain.edition_render import (
    EditionRender,
    EditionRenderAcquisition,
    EditionRenderAcquisitionOutcome,
    EditionRenderFormat,
    EditionRenderStatus,
)
from cti_app.domain.editions import Edition
from cti_app.domain.production import EditionProductionBatch, ProductionBatchStatus
from cti_app.domain.publication_document import PublicationDocumentV4
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CHP_TYPST_ROOT = REPO_ROOT / "chpTypst"
FONT_BUNDLE_ROOT = Path(os.environ.get("FONT_BUNDLE_ROOT") or CHP_TYPST_ROOT)


async def _persist_release(uow_factory: UnitOfWorkFactory) -> EditionRelease:
    token = uuid4().int
    year = 2000 + token % 6000
    month = (token >> 16) % 12 + 1
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(year, month, 1),
        period_end=date(year, month, calendar.monthrange(year, month)[1]),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status=ProductionBatchStatus.QUEUED,
        idempotency_key=f"edition-render-{uuid4().hex}",
    )
    manifest = PublicationManifestV1.create(
        edition_id=edition.id,
        edition_version=edition.version,
        batch_id=batch.id,
        created_by="edition-render-postgres-test",
        entries=(),
        exclusions=(),
    )
    manifest_content = manifest.content_sha256.encode()
    manifest_blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=sha256(manifest_content).hexdigest(),
            size=len(manifest_content),
            mime_type="application/octet-stream",
            logical_bucket="edition-render-postgres-manifest",
        )
    )
    document_content = uuid4().bytes
    document_blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=sha256(document_content).hexdigest(),
            size=len(document_content),
            mime_type="application/octet-stream",
            logical_bucket="edition-render-postgres-document",
        )
    )
    release = EditionRelease(
        edition_id=edition.id,
        manifest_id=manifest.id,
        edition_document_blob_id=document_blob.id,
        edition_document_sha256=sha256(document_content).hexdigest(),
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.edition_production_batches.add(batch)
        await uow.blobs.add(manifest_blob)
        await uow.blobs.add(document_blob)
        await uow.publication_manifests.add(manifest, manifest_blob.id)
        assert await uow.edition_releases.add_if_absent(release)
        await uow.commit()
    return release


class _CountingEditionCompiler:
    def __init__(
        self,
        *,
        failures: tuple[Exception, ...] = (),
        compile_started: asyncio.Event | None = None,
        release_compile: asyncio.Event | None = None,
    ) -> None:
        self.failures = list(failures)
        self.compile_started = compile_started
        self.release_compile = release_compile
        self.call_count = 0
        self.content = f"%PDF-edition-integration-{uuid4().hex}".encode()

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.call_count += 1
        assert (request.workspace_root / request.entrypoint_relative_path).is_file()
        if self.compile_started is not None:
            self.compile_started.set()
        if self.release_compile is not None:
            await self.release_compile.wait()
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


def _edition_document(edition: Edition, *, title: str) -> EditionDocumentV2:
    subject_id = uuid4()
    publication = PublicationDocumentV4(
        schema_version="4",
        subject_id=subject_id,
        publication_language="fr",
        title=title,
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
    return EditionDocumentV2(
        edition={
            "id": str(edition.id),
            "country": edition.country,
            "country_code": edition.country_code,
            "period_start": edition.period_start.isoformat(),
            "period_end": edition.period_end.isoformat(),
            "tlp": edition.tlp.value,
            "languages": list(edition.languages),
            "version": edition.version,
        },
        publications=(
            EditionPublicationV2(
                position=1,
                subject_id=subject_id,
                document=publication,
            ),
        ),
    )


async def _persist_renderable_release(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
) -> tuple[EditionRelease, EditionDocumentV2]:
    token = uuid4().int
    year = 2000 + token % 6000
    month = (token >> 16) % 12 + 1
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(year, month, 1),
        period_end=date(year, month, calendar.monthrange(year, month)[1]),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status=ProductionBatchStatus.QUEUED,
        idempotency_key=f"edition-render-service-{uuid4().hex}",
    )
    manifest = PublicationManifestV1.create(
        edition_id=edition.id,
        edition_version=edition.version,
        batch_id=batch.id,
        created_by="edition-render-postgres-test",
        entries=(),
        exclusions=(),
    )
    manifest_blob_id, _ = await artifact_store.put_canonical_json(
        manifest.to_json(), bucket="edition-render-test-manifests"
    )
    document = _edition_document(edition, title=f"Frozen {uuid4().hex}")
    document_blob_id, document_sha256 = await artifact_store.put_canonical_json(
        document.to_json(), bucket="edition-render-test-documents"
    )
    release = EditionRelease(
        edition_id=edition.id,
        manifest_id=manifest.id,
        edition_document_blob_id=document_blob_id,
        edition_document_sha256=document_sha256,
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.edition_production_batches.add(batch)
        await uow.commit()
    async with uow_factory() as uow:
        await uow.publication_manifests.add(manifest, manifest_blob_id)
        assert await uow.edition_releases.add_if_absent(release)
        await uow.commit()
    return release, document


def _render_service(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    compiler: _CountingEditionCompiler,
) -> EditionRenderService:
    return EditionRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=MediaAssetStore(artifact_store, uow_factory),
        renderer=EditionTypstRenderer(),
        compiler=compiler,
        chp_typst_root=CHP_TYPST_ROOT,
        font_bundle_root=FONT_BUNDLE_ROOT,
        typst_fonts_lock_path=REPO_ROOT / "infra" / "typst-fonts.lock",
    )


def _render(release_id: UUID, **overrides: object) -> EditionRender:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "edition_release_id": release_id,
        "renderer": "typst",
        "renderer_version": "edition-v2-typst-v1",
        "template_version": "chp-edition-v1",
        "template_sha256": "a" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "font_bundle_version": "test-font-bundle-v1",
        "render_policy_version": "typst-edition-v2-v1",
        "format": EditionRenderFormat.PDF,
        "input_hash": sha256(uuid4().bytes).hexdigest(),
        "source_blob_id": None,
        "render_data_blob_id": None,
        "output_blob_id": None,
        "output_sha256": None,
        "output_byte_size": None,
        "status": EditionRenderStatus.RUNNING,
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return EditionRender(**values)  # type: ignore[arg-type]


async def _acquire_concurrently(
    uow_factory: UnitOfWorkFactory,
    proposed: EditionRender,
) -> list[EditionRenderAcquisition]:
    async def acquire() -> EditionRenderAcquisition:
        async with uow_factory() as uow:
            result = await uow.edition_renders.acquire_for_render(
                proposed,
                stale_running_before=datetime.now(UTC) - timedelta(minutes=5),
            )
            await uow.commit()
            return result

    return await asyncio.gather(*(acquire() for _ in range(8)))


async def test_concurrent_acquire_for_render_has_one_owner(
    uow_factory: UnitOfWorkFactory,
) -> None:
    release = await _persist_release(uow_factory)
    proposed = _render(release.id)

    acquisitions = await _acquire_concurrently(uow_factory, proposed)

    assert (
        sum(
            acquisition.outcome is EditionRenderAcquisitionOutcome.ACQUIRED
            for acquisition in acquisitions
        )
        == 1
    )
    assert (
        sum(
            acquisition.outcome is EditionRenderAcquisitionOutcome.IN_PROGRESS
            for acquisition in acquisitions
        )
        == len(acquisitions) - 1
    )
    assert len({acquisition.render.id for acquisition in acquisitions}) == 1
    async with uow_factory() as uow:
        persisted = await uow.edition_renders.get_by_input_hash(proposed.input_hash)
    assert persisted is not None
    assert persisted.status is EditionRenderStatus.RUNNING


async def test_concurrent_retry_of_failed_render_has_one_owner(
    uow_factory: UnitOfWorkFactory,
) -> None:
    release = await _persist_release(uow_factory)
    proposed = _render(release.id)
    async with uow_factory() as uow:
        await uow.edition_renders.acquire_for_render(
            proposed, stale_running_before=datetime.now(UTC)
        )
        await uow.edition_renders.mark_failed(
            proposed.id, error_code="typst_compile_failed", error_message="initial failure"
        )
        await uow.commit()

    acquisitions = await _acquire_concurrently(uow_factory, proposed)
    assert (
        sum(
            acquisition.outcome is EditionRenderAcquisitionOutcome.ACQUIRED
            for acquisition in acquisitions
        )
        == 1
    )
    assert (
        sum(
            acquisition.outcome is EditionRenderAcquisitionOutcome.IN_PROGRESS
            for acquisition in acquisitions
        )
        == len(acquisitions) - 1
    )
    assert all(acquisition.render.id == proposed.id for acquisition in acquisitions)


async def test_concurrent_invalid_succeeded_reacquire_has_one_owner(
    uow_factory: UnitOfWorkFactory,
) -> None:
    release = await _persist_release(uow_factory)
    output_content = uuid4().bytes
    output_blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=sha256(output_content).hexdigest(),
            size=len(output_content),
            mime_type="application/pdf",
            logical_bucket="edition-render-postgres-output",
        )
    )
    proposed = _render(release.id)
    async with uow_factory() as uow:
        await uow.blobs.add(output_blob)
        await uow.edition_renders.acquire_for_render(
            proposed, stale_running_before=datetime.now(UTC)
        )
        succeeded = await uow.edition_renders.mark_succeeded(
            proposed.id,
            output_blob_id=output_blob.id,
            output_sha256=sha256(output_content).hexdigest(),
            output_byte_size=len(output_content),
        )
        await uow.commit()

    invalid = replace(succeeded, status=EditionRenderStatus.RUNNING)

    async def reacquire() -> EditionRenderAcquisition:
        async with uow_factory() as uow:
            result = await uow.edition_renders.reacquire_invalid_succeeded(
                invalid,
                observed_output_blob_id=output_blob.id,
                observed_output_sha256=succeeded.output_sha256,
            )
            await uow.commit()
            return result

    acquisitions = await asyncio.gather(*(reacquire() for _ in range(8)))
    assert (
        sum(
            acquisition.outcome is EditionRenderAcquisitionOutcome.ACQUIRED
            for acquisition in acquisitions
        )
        == 1
    )
    assert (
        sum(
            acquisition.outcome is EditionRenderAcquisitionOutcome.IN_PROGRESS
            for acquisition in acquisitions
        )
        == len(acquisitions) - 1
    )
    assert all(acquisition.render.id == succeeded.id for acquisition in acquisitions)


@pytest.mark.asyncio
async def test_concurrent_render_pdf_compiles_once_and_persists_one_row(
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "edition-render-blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    release, _document = await _persist_renderable_release(uow_factory, artifact_store)
    started = asyncio.Event()
    release_compile = asyncio.Event()
    compiler = _CountingEditionCompiler(
        compile_started=started,
        release_compile=release_compile,
    )
    service = _render_service(uow_factory, artifact_store, compiler)

    first_task = asyncio.create_task(service.render_pdf(release.id))
    await asyncio.wait_for(started.wait(), timeout=10)
    second_task = asyncio.create_task(service.render_pdf(release.id))
    await asyncio.sleep(0.05)
    release_compile.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.id == second.id
    assert compiler.call_count == 1
    async with uow_factory() as uow:
        persisted = await uow.edition_renders.get_latest_for_release(release.id)
    assert persisted is not None
    assert persisted.id == first.id
    assert persisted.status is EditionRenderStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_corrupted_render_pdf_blob_is_reacquired_and_rerendered(
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    blob_root = tmp_path / f"edition-render-corrupt-{uuid4().hex}"
    blob_store = FilesystemBlobStore(blob_root)
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    release, _document = await _persist_renderable_release(uow_factory, artifact_store)
    compiler = _CountingEditionCompiler()
    service = _render_service(uow_factory, artifact_store, compiler)
    first = await service.render_pdf(release.id)
    assert first.output_blob_id is not None
    async with uow_factory() as uow:
        output_blob = await uow.blobs.get(first.output_blob_id)
    assert output_blob is not None
    (blob_root / output_blob.descriptor.object_key).write_bytes(b"%PDF-corrupted-by-test")
    compiler.content = f"%PDF-restored-{uuid4().hex}".encode()

    second = await service.render_pdf(release.id)

    assert second.id == first.id
    assert second.status is EditionRenderStatus.SUCCEEDED
    assert second.output_sha256 == sha256(compiler.content).hexdigest()
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_failed_render_keeps_release_and_retry_reacquires_same_row(
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / f"edition-render-retry-{uuid4().hex}")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    release, _document = await _persist_renderable_release(uow_factory, artifact_store)
    compiler = _CountingEditionCompiler(
        failures=(TypstCompileFailedError("fixture compile failure"),)
    )
    service = _render_service(uow_factory, artifact_store, compiler)

    with pytest.raises(TypstCompileFailedError):
        await service.render_pdf(release.id)
    async with uow_factory() as uow:
        failed = await uow.edition_renders.get_latest_for_release(release.id)
        persisted_release = await uow.edition_releases.get(release.id)
    assert failed is not None
    assert failed.status is EditionRenderStatus.FAILED
    assert persisted_release == release

    retried = await service.render_pdf(release.id)

    assert retried.id == failed.id
    assert retried.status is EditionRenderStatus.SUCCEEDED
    async with uow_factory() as uow:
        assert await uow.edition_releases.get(release.id) == release

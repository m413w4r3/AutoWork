"""PostgreSQL/blob-store coverage for PublicationRenderService orchestration."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import ipaddress
import json
import os
import shutil
import socket
from dataclasses import replace
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pypdf import PdfReader
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from cti_app.api.subject_content import router as subject_content_router
from cti_app.application import publication_rendering
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.edition_publication import EditionPublicationService
from cti_app.application.edition_rendering import EditionRenderService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.subject_content import SubjectContentService
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    TypstCompileFailedError,
    TypstCompiler,
    TypstCompileRequest,
    load_font_bundle_snapshot,
)
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionPublicationV2,
    EditionRelease,
    PublicationManifestEntryV1,
    PublicationManifestV1,
)
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionBatchStatus,
    ProductionRun,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    publication_document_v4_to_json,
)
from cti_app.domain.publication_render import (
    PUBLICATION_RENDER_POLICY_VERSION,
    PublicationRender,
    compute_publication_render_input_hash,
)
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.edition_render import EditionRenderRow
from cti_app.infrastructure.database.models.publication_render import PublicationRenderRow
from cti_app.infrastructure.database.session import create_postgres_engine
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_publication_v4 import _diagram
from tests.test_publication_v4 import _document as fixture_document

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CHP_TYPST_ROOT = REPO_ROOT / "chpTypst"
FONT_BUNDLE_ROOT = Path(os.environ.get("FONT_BUNDLE_ROOT") or CHP_TYPST_ROOT)


class ControlledTypstCompiler:
    def __init__(
        self,
        failures: tuple[Exception, ...] = (),
        *,
        compile_started: asyncio.Event | None = None,
        release_compile: asyncio.Event | None = None,
    ) -> None:
        self.failures = list(failures)
        self.call_count = 0
        self.content = b"%PDF-integration-publication-render"
        self.compile_started = compile_started
        self.release_compile = release_compile

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


def _minimal_document(subject_id: UUID) -> PublicationDocumentV4:
    return PublicationDocumentV4(
        schema_version="4",
        subject_id=subject_id,
        publication_language="fr",
        title=f"PostgreSQL render fixture {uuid4().hex}",
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


async def _seed_verified_publication(
    *,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    with_missing_diagram: bool = False,
) -> tuple[ProductionRun, ProductionArtifact, PublicationDocumentV4]:
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
    subject = Subject(
        edition_id=edition.id,
        title="Publication render integration fixture",
        slug=f"publication-render-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    if with_missing_diagram:
        diagram = _diagram(asset_id=uuid4())
        document = replace(
            fixture_document(diagrams=(diagram,)),
            subject_id=subject.id,
        )
    else:
        document = _minimal_document(subject.id)
    canonical_blob_id = await artifact_store.put_json(
        publication_document_v4_to_json(document),
        bucket="publication-render-test-canonical",
    )
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_blob_id,
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.commit()
    return run, artifact, document


def _service(
    *,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    compiler: TypstCompiler,
    chp_typst_root: Path = CHP_TYPST_ROOT,
    font_bundle_root: Path = FONT_BUNDLE_ROOT,
    typst_fonts_lock_path: Path = REPO_ROOT / "infra" / "typst-fonts.lock",
) -> publication_rendering.PublicationRenderService:
    return publication_rendering.PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=MediaAssetStore(artifact_store, uow_factory),
        renderer=TypstRenderer(),
        compiler=compiler,
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
    )


async def _persist_release_for_publication(
    *,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    edition: Edition,
    run: ProductionRun,
    artifact: ProductionArtifact,
    document: PublicationDocumentV4,
    created_at: datetime | None = None,
) -> tuple[EditionRelease, PublicationManifestV1, str]:
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status=ProductionBatchStatus.COMPLETED,
        phase=ProductionBatchPhase.REVIEW,
        idempotency_key=f"immutable-release-{uuid4().hex}",
        actor_id="release-immutability-test",
        correlation_id=f"release-immutability-{uuid4().hex}",
    )
    entry = PublicationManifestEntryV1(
        position=1,
        subject_id=artifact.subject_id,
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        document_artifact_id=artifact.id,
        document_artifact_version=artifact.version,
        document_input_hash=artifact.input_hash,
    )
    manifest = PublicationManifestV1.create(
        edition_id=edition.id,
        edition_version=edition.version,
        batch_id=batch.id,
        created_by="release-immutability-test",
        entries=(entry,),
        exclusions=(),
        created_at=created_at,
    )
    manifest_blob_id, manifest_sha256 = await artifact_store.put_canonical_json(
        manifest.to_json(), bucket="release-immutability-manifests"
    )
    edition_document = EditionDocumentV2(
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
                subject_id=artifact.subject_id,
                document=document,
            ),
        ),
    )
    edition_document_blob_id, edition_document_sha256 = await artifact_store.put_canonical_json(
        edition_document.to_json(), bucket="release-immutability-documents"
    )
    release = EditionRelease(
        edition_id=edition.id,
        manifest_id=manifest.id,
        edition_document_blob_id=edition_document_blob_id,
        edition_document_sha256=edition_document_sha256,
    )
    async with uow_factory() as uow:
        await uow.edition_production_batches.add(batch)
        await uow.commit()
        await uow.edition_production_batch_items.append_many(
            (
                EditionProductionBatchItem(
                    batch_id=batch.id,
                    subject_id=artifact.subject_id,
                    production_run_id=run.id,
                    position=1,
                ),
            )
        )
        await uow.commit()
    async with uow_factory() as uow:
        await uow.publication_manifests.add(manifest, manifest_blob_id)
        await uow.commit()
        await uow.publication_manifest_entries.append_many(manifest.id, manifest.entries)
        await uow.publication_manifest_exclusions.append_many(manifest.id, manifest.exclusions)
        assert await uow.edition_releases.add_if_absent(release)
        await uow.commit()
    return release, manifest, manifest_sha256


async def _served_article_pdf(
    *,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    publication_renderer: publication_rendering.PublicationRenderService,
    subject_id: UUID,
):
    app = FastAPI()
    app.include_router(subject_content_router)
    app.state.subject_content_service = SubjectContentService(uow_factory, artifact_store)
    app.state.publication_render_service = publication_renderer
    app.state.production_artifact_store = artifact_store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"/api/subjects/{subject_id}/publication/pdf")


async def _render_status_rows(
    database_url: str, publication_artifact_id: UUID
) -> list[tuple[str, str | None]]:
    engine = create_postgres_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT status, error_code FROM publication_renders "
                    "WHERE publication_artifact_id = :artifact_id ORDER BY created_at, id"
                ),
                {"artifact_id": publication_artifact_id},
            )
            return [(str(row.status), row.error_code) for row in result]
    finally:
        await engine.dispose()


async def _render_identity_rows(
    database_url: str, input_hash: str
) -> list[tuple[UUID, str, str | None]]:
    engine = create_postgres_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT id, status, output_sha256 FROM publication_renders "
                    "WHERE input_hash = :input_hash"
                ),
                {"input_hash": input_hash},
            )
            return [(row.id, str(row.status), row.output_sha256) for row in result]
    finally:
        await engine.dispose()


async def _render_blob_reference_rows(
    database_url: str, publication_artifact_id: UUID
) -> list[tuple[UUID, str, UUID | None, UUID | None, UUID | None]]:
    engine = create_postgres_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT id, status, source_blob_id, render_data_blob_id, output_blob_id "
                    "FROM publication_renders WHERE publication_artifact_id = :artifact_id"
                ),
                {"artifact_id": publication_artifact_id},
            )
            return [
                (
                    row.id,
                    str(row.status),
                    row.source_blob_id,
                    row.render_data_blob_id,
                    row.output_blob_id,
                )
                for row in result
            ]
    finally:
        await engine.dispose()


async def _persisted_blob_ids(database_url: str, blob_ids: tuple[UUID, ...]) -> set[UUID]:
    engine = create_postgres_engine(database_url)
    try:
        async with engine.connect() as connection:
            present: set[UUID] = set()
            for blob_id in blob_ids:
                found = await connection.scalar(
                    text("SELECT id FROM blobs WHERE id = :blob_id"), {"blob_id": blob_id}
                )
                if found is not None:
                    present.add(found)
            return present
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publication_render_postgres_success_idempotency_and_template_pin(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    template_root = tmp_path / "chpTypst"
    shutil.copytree(CHP_TYPST_ROOT, template_root)
    changed_template_path = template_root / "UTILS" / "helpers.typ"
    original_template_bytes = changed_template_path.read_bytes()
    run, artifact, _ = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    assert artifact.canonical_blob_id is not None
    async with uow_factory() as uow:
        before_artifact = await uow.production_artifacts.get(artifact.id)
        before_run = await uow.production_runs.get(run.id)
        before_canonical_blob = await uow.blobs.get(artifact.canonical_blob_id)
    before_canonical_bytes = await artifact_store.read_bytes(artifact.canonical_blob_id)
    compiler = ControlledTypstCompiler()
    service = _service(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        compiler=compiler,
        chp_typst_root=template_root,
    )

    first = await service.render_pdf(artifact.id)
    repeated = await service.render_pdf(artifact.id)

    assert first.status is TypstRenderStatus.SUCCEEDED
    assert first.source_blob_id is not None
    assert first.render_data_blob_id is not None
    assert first.output_blob_id is not None
    assert await artifact_store.read_bytes(first.source_blob_id)
    assert await artifact_store.read_bytes(first.render_data_blob_id)
    assert await artifact_store.read_bytes(first.output_blob_id) == compiler.content
    assert repeated == first
    assert compiler.call_count == 1

    changed = original_template_bytes[:-1] + bytes([original_template_bytes[-1] ^ 1])
    changed_template_path.write_bytes(changed)
    changed_template = await service.render_pdf(artifact.id)

    assert changed_template.id == first.id
    assert changed_template.input_hash == first.input_hash
    assert changed_template.template_sha256 == first.template_sha256
    assert changed_template.font_bundle_version == first.font_bundle_version
    assert changed_template.render_policy_version == first.render_policy_version
    assert changed_template.publication_artifact_id == first.publication_artifact_id == artifact.id
    assert compiler.call_count == 1
    async with uow_factory() as uow:
        assert await uow.production_artifacts.get(artifact.id) == before_artifact
        assert await uow.production_runs.get(run.id) == before_run
        assert await uow.blobs.get(artifact.canonical_blob_id) == before_canonical_blob
    assert await artifact_store.read_bytes(artifact.canonical_blob_id) == before_canonical_bytes
    assert await _render_status_rows(migrated_postgres_url, artifact.id) == [("succeeded", None)]


@pytest.mark.asyncio
async def test_publication_render_postgres_font_only_change_keeps_pinned_identity(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    font_root = tmp_path / "fonts"
    shutil.copytree(FONT_BUNDLE_ROOT, font_root)
    font_lock = tmp_path / "typst-fonts.lock"
    shutil.copyfile(REPO_ROOT / "infra" / "typst-fonts.lock", font_lock)
    initial_font_bundle = load_font_bundle_snapshot(font_root, font_lock)
    assert initial_font_bundle.files
    font_relative_path, original_font = initial_font_bundle.files[0]
    assert original_font

    run, artifact, document = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    async with uow_factory() as uow:
        before_artifact = await uow.production_artifacts.get(artifact.id)
        before_run = await uow.production_runs.get(run.id)
        before_canonical_blob = await uow.blobs.get(artifact.canonical_blob_id)
    canonical_bytes = await artifact_store.read_bytes(artifact.canonical_blob_id)
    compiler = ControlledTypstCompiler()
    service = _service(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        compiler=compiler,
        font_bundle_root=font_root,
        typst_fonts_lock_path=font_lock,
    )

    first = await service.render_pdf(artifact.id)

    path = font_root.joinpath(*font_relative_path.split("/"))
    changed_font = original_font[:-1] + bytes([original_font[-1] ^ 1])
    path.write_bytes(changed_font)
    changed_font_bundle = load_font_bundle_snapshot(font_root, font_lock)
    assert changed_font_bundle.font_bundle_version != initial_font_bundle.font_bundle_version
    second = await service.render_pdf(artifact.id)

    assert second.id == first.id
    assert second.input_hash == first.input_hash
    assert first.font_bundle_version == initial_font_bundle.font_bundle_version
    assert second.font_bundle_version == initial_font_bundle.font_bundle_version
    assert second.template_sha256 == first.template_sha256
    assert second.render_policy_version == first.render_policy_version
    assert second.publication_artifact_id == first.publication_artifact_id == artifact.id
    assert compiler.call_count == 1
    async with uow_factory() as uow:
        assert await uow.production_artifacts.get(artifact.id) == before_artifact
        assert await uow.production_runs.get(run.id) == before_run
        assert await uow.blobs.get(artifact.canonical_blob_id) == before_canonical_blob
    assert await artifact_store.read_bytes(artifact.canonical_blob_id) == canonical_bytes
    assert canonical_bytes == ProductionArtifactStore.canonical_json_bytes(
        publication_document_v4_to_json(document)
    )
    assert await _render_status_rows(migrated_postgres_url, artifact.id) == [("succeeded", None)]


@pytest.mark.asyncio
async def test_publication_render_postgres_real_typst_exit_scenario(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typst_binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    assert typst_binary is not None, "TYPST_BINARY must point to the pinned Typst 0.15.1 compiler"

    # The test's PostgreSQL endpoint is the only permitted network destination.
    database_url = make_url(migrated_postgres_url)
    allowed_database_hosts: set[str] = set()
    if database_url.host is not None:
        allowed_database_hosts.add(database_url.host)
        try:
            allowed_database_hosts.update(
                str(result[4][0])
                for result in socket.getaddrinfo(
                    database_url.host, database_url.port, type=socket.SOCK_STREAM
                )
            )
        except socket.gaierror:
            pass

    def permitted_address(address: object) -> bool:
        if not isinstance(address, tuple) or not address:
            return True  # Unix sockets remain local.
        host = address[0]
        if not isinstance(host, str):
            return False
        if host in allowed_database_hosts or host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host.split("%", maxsplit=1)[0]).is_loopback
        except ValueError:
            return False

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def block_external_connect(sock: socket.socket, address: object) -> object:
        if not permitted_address(address):
            raise OSError("Outbound network is disabled in the publication render test")
        return real_connect(sock, address)  # type: ignore[arg-type]

    def block_external_connect_ex(sock: socket.socket, address: object) -> int:
        if not permitted_address(address):
            raise OSError("Outbound network is disabled in the publication render test")
        return real_connect_ex(sock, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", block_external_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", block_external_connect_ex)

    from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler

    def unexpected_path(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("The Typst publication render called a legacy compiler path")

    monkeypatch.setattr(D2DiagramCompiler, "compile", unexpected_path)

    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    run, artifact, document = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    assert artifact.canonical_blob_id is not None
    async with uow_factory() as uow:
        before_artifact = await uow.production_artifacts.get(artifact.id)
        before_run = await uow.production_runs.get(run.id)
        before_canonical_blob = await uow.blobs.get(artifact.canonical_blob_id)
    before_canonical_bytes = await artifact_store.read_bytes(artifact.canonical_blob_id)
    assert before_artifact is not None
    assert before_artifact.rendered_blob_id is None

    class RunningStatusCompiler:
        def __init__(self) -> None:
            self.delegate = TypstSubprocessCompiler(binary=typst_binary)
            self.status_when_compiling: list[tuple[str, str | None]] = []

        async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
            self.status_when_compiling = await _render_status_rows(
                migrated_postgres_url, artifact.id
            )
            assert self.status_when_compiling == [("running", None)]
            return await self.delegate.compile(request)

    compiler = RunningStatusCompiler()
    service = _service(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        compiler=compiler,  # type: ignore[arg-type]
    )

    render = await service.render_pdf(artifact.id)

    assert render.status is TypstRenderStatus.SUCCEEDED
    assert (
        render.font_bundle_version
        == load_font_bundle_snapshot(
            FONT_BUNDLE_ROOT, REPO_ROOT / "infra" / "typst-fonts.lock"
        ).font_bundle_version
    )
    assert render.render_policy_version == PUBLICATION_RENDER_POLICY_VERSION
    assert render.input_hash == compute_publication_render_input_hash(
        publication_artifact_id=artifact.id,
        publication_content_sha256=hashlib.sha256(before_canonical_bytes).hexdigest(),
        renderer=render.renderer,
        renderer_version=render.renderer_version,
        template_version=render.template_version,
        template_sha256=render.template_sha256,
        compiler=render.compiler,
        compiler_version=render.compiler_version,
        font_bundle_version=render.font_bundle_version,
        render_policy_version=render.render_policy_version,
        format=TypstRenderFormat.PDF,
    )
    assert render.source_blob_id is not None
    assert render.render_data_blob_id is not None
    assert render.output_blob_id is not None
    assert await _persisted_blob_ids(
        migrated_postgres_url,
        (render.source_blob_id, render.render_data_blob_id, render.output_blob_id),
    ) == {render.source_blob_id, render.render_data_blob_id, render.output_blob_id}
    assert await _render_status_rows(migrated_postgres_url, artifact.id) == [("succeeded", None)]
    assert await _render_blob_reference_rows(migrated_postgres_url, artifact.id) == [
        (
            render.id,
            "succeeded",
            render.source_blob_id,
            render.render_data_blob_id,
            render.output_blob_id,
        )
    ]

    pdf_bytes = await artifact_store.read_bytes(render.output_blob_id)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(PdfReader(BytesIO(pdf_bytes), strict=True).pages) >= 1
    async with uow_factory() as uow:
        assert await uow.production_artifacts.get(artifact.id) == before_artifact
        assert await uow.production_runs.get(run.id) == before_run
        assert await uow.blobs.get(artifact.canonical_blob_id) == before_canonical_blob
    assert await artifact_store.read_bytes(artifact.canonical_blob_id) == before_canonical_bytes
    assert before_canonical_bytes == ProductionArtifactStore.canonical_json_bytes(
        publication_document_v4_to_json(document)
    )
    async with uow_factory() as uow:
        after_artifact = await uow.production_artifacts.get(artifact.id)
    assert after_artifact is not None
    assert after_artifact.rendered_blob_id is None


@pytest.mark.asyncio
async def test_publication_render_postgres_concurrent_callers_compile_once(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    _, artifact, _ = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    compile_started = asyncio.Event()
    release_compile = asyncio.Event()
    compiler = ControlledTypstCompiler(
        compile_started=compile_started,
        release_compile=release_compile,
    )
    service = _service(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        compiler=compiler,
    )
    caller_count = 0
    both_callers_started = asyncio.Event()

    async def render_after_barrier() -> PublicationRender:
        nonlocal caller_count
        caller_count += 1
        if caller_count == 2:
            both_callers_started.set()
        await both_callers_started.wait()
        return await service.render_pdf(artifact.id)

    concurrent_calls = asyncio.gather(render_after_barrier(), render_after_barrier())
    await compile_started.wait()
    # Keep the owning worker in compilation while the second service call
    # reaches the committed RUNNING row and waits for it.
    await asyncio.sleep(0.2)
    release_compile.set()
    first, second = await concurrent_calls

    assert first == second
    assert first.status is TypstRenderStatus.SUCCEEDED
    assert compiler.call_count == 1
    assert first.output_sha256 == second.output_sha256
    rows = await _render_identity_rows(migrated_postgres_url, first.input_hash)
    assert rows == [(first.id, "succeeded", first.output_sha256)]


@pytest.mark.asyncio
async def test_publication_render_postgres_retries_failed_input_in_same_row(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    _, artifact, _ = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    compiler_failure = TypstCompileFailedError("controlled first failure")
    compiler = ControlledTypstCompiler((compiler_failure,))
    service = _service(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        compiler=compiler,
    )

    with pytest.raises(TypstCompileFailedError) as raised:
        await service.render_pdf(artifact.id)
    assert raised.value is compiler_failure
    assert await _render_status_rows(migrated_postgres_url, artifact.id) == [
        ("failed", "typst_compile_failed")
    ]

    retried = await service.render_pdf(artifact.id)

    assert retried.status is TypstRenderStatus.SUCCEEDED
    assert retried.error_code is None
    assert compiler.call_count == 2
    assert await _render_status_rows(migrated_postgres_url, artifact.id) == [("succeeded", None)]


@pytest.mark.asyncio
async def test_publication_render_postgres_missing_media_only_fails_render(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    run, artifact, _ = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        with_missing_diagram=True,
    )
    async with uow_factory() as uow:
        before_artifact = await uow.production_artifacts.get(artifact.id)
        before_run = await uow.production_runs.get(run.id)
    compiler = ControlledTypstCompiler()
    service = _service(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        compiler=compiler,
    )

    with pytest.raises(publication_rendering.PublicationRenderMediaMissingError) as raised:
        await service.render_pdf(artifact.id)

    assert raised.value.code == "publication_render_media_missing"
    assert compiler.call_count == 0
    async with uow_factory() as uow:
        assert await uow.production_artifacts.get(artifact.id) == before_artifact
        assert await uow.production_runs.get(run.id) == before_run
    assert await _render_status_rows(migrated_postgres_url, artifact.id) == [
        ("failed", "publication_render_media_missing")
    ]


@pytest.mark.asyncio
async def test_postgres_real_typst_release_pins_survive_bundle_bump_and_explicit_repin(
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
) -> None:
    typst_binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    if typst_binary is None:
        pytest.skip("Typst compiler capability is unavailable")
    version_process = await asyncio.create_subprocess_exec(
        typst_binary,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    version_stdout, _version_stderr = await asyncio.wait_for(version_process.communicate(), 10)
    assert version_process.returncode == 0
    assert version_stdout.decode("utf-8", errors="replace").strip().startswith("typst 0.15.1")

    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "release-pin-blobs"), uow_factory)
    )
    template_root = tmp_path / "chpTypst"
    shutil.copytree(CHP_TYPST_ROOT, template_root)
    font_root = tmp_path / "fonts"
    shutil.copytree(FONT_BUNDLE_ROOT, font_root)
    font_lock = tmp_path / "typst-fonts.lock"
    shutil.copyfile(REPO_ROOT / "infra" / "typst-fonts.lock", font_lock)
    compiler = TypstSubprocessCompiler(binary=typst_binary)
    publication_renderer = _service(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        compiler=compiler,
        chp_typst_root=template_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=font_lock,
    )

    run, artifact, document = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    async with uow_factory() as uow:
        edition = await uow.editions.get(run.edition_id)
    assert edition is not None
    release, manifest, recorded_manifest_sha256 = await _persist_release_for_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        edition=edition,
        run=run,
        artifact=artifact,
        document=document,
    )
    async with uow_factory() as uow:
        r1_manifest_blob_id = await uow.publication_manifests.get_blob_id(manifest.id)
    assert r1_manifest_blob_id is not None
    r1_manifest_bytes = await artifact_store.read_bytes(r1_manifest_blob_id)
    assert hashlib.sha256(r1_manifest_bytes).hexdigest() == recorded_manifest_sha256

    edition_renderer = EditionRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=MediaAssetStore(artifact_store, uow_factory),
        renderer=EditionTypstRenderer(),
        compiler=compiler,
        chp_typst_root=template_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=font_lock,
    )
    edition_publication = EditionPublicationService(uow_factory, artifact_store)
    article_r1_response = await _served_article_pdf(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        publication_renderer=publication_renderer,
        subject_id=artifact.subject_id,
    )
    assert article_r1_response.status_code == 200, article_r1_response.text
    assert article_r1_response.headers["x-publication-artifact-version"] == "1"
    article_r1_bytes = article_r1_response.content
    async with uow_factory() as uow:
        article_r1 = await uow.publication_renders.get_pinned_succeeded_for_artifact(artifact.id)
    assert article_r1 is not None
    edition_r1 = await edition_renderer.render_pdf(release.id)
    assert article_r1.output_blob_id is not None
    assert edition_r1.output_sha256 is not None
    assert await artifact_store.read_bytes(article_r1.output_blob_id) == article_r1_bytes
    served_edition_r1 = await edition_publication.read_release_pdf(edition.id)
    assert served_edition_r1.content.startswith(b"%PDF-")
    assert len(PdfReader(BytesIO(article_r1_bytes), strict=True).pages) >= 1
    assert len(PdfReader(BytesIO(served_edition_r1.content), strict=True).pages) >= 1
    article_r1_sha256 = hashlib.sha256(article_r1_bytes).hexdigest()
    edition_r1_sha256 = hashlib.sha256(served_edition_r1.content).hexdigest()
    assert article_r1_sha256 == article_r1.output_sha256
    assert edition_r1_sha256 == edition_r1.output_sha256

    article_manifest_path = template_root / "renderer-manifest.json"
    edition_manifest_path = template_root / "edition-renderer-manifest.json"
    for manifest_path in (article_manifest_path, edition_manifest_path):
        bundle_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        bundle_manifest["template_version"] += "-explicit-repin-test"
        manifest_path.write_text(
            json.dumps(bundle_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    article_entrypoint = template_root / "RENDERER" / "publication.typ"
    article_source = article_entrypoint.read_text(encoding="utf-8")
    assert article_source.count("#render-publication(publication)") == 1
    article_entrypoint.write_text(
        article_source.replace(
            "#render-publication(publication)",
            '#render-publication(publication)\n  #v(8pt)\n  #text("BUNDLE R2 TEST")',
        ),
        encoding="utf-8",
    )
    edition_entrypoint = template_root / "RENDERER" / "edition.typ"
    edition_source = edition_entrypoint.read_text(encoding="utf-8")
    assert edition_source.count("#render-edition(edition)") == 1
    edition_entrypoint.write_text(
        edition_source.replace(
            "#render-edition(edition)",
            '#render-edition(edition)\n  #v(8pt)\n  #text("BUNDLE R2 TEST")',
        ),
        encoding="utf-8",
    )

    # Model a later successful row left by the previous latest-row behavior.
    legacy_article_bytes = b"%PDF-legacy-unpinned-article-render"
    legacy_article_blob_id = await artifact_store.put_bytes(
        legacy_article_bytes,
        bucket="publication-renders-output",
        mime_type="application/pdf",
    )
    legacy_article_created_at = article_r1.created_at + timedelta(seconds=10)
    legacy_article = replace(
        article_r1,
        id=uuid4(),
        template_version=article_r1.template_version + "-legacy-current-bundle",
        template_sha256=hashlib.sha256(b"legacy article template").hexdigest(),
        input_hash=hashlib.sha256(b"legacy article input identity").hexdigest(),
        source_blob_id=None,
        render_data_blob_id=None,
        output_blob_id=None,
        output_sha256=None,
        output_byte_size=None,
        status=TypstRenderStatus.RUNNING,
        error_code=None,
        error_message=None,
        created_at=legacy_article_created_at,
        updated_at=legacy_article_created_at,
    )
    async with uow_factory() as uow:
        acquired_article = await uow.publication_renders.acquire_for_render(
            legacy_article,
            stale_running_before=datetime.now().astimezone(),
        )
        await uow.publication_renders.mark_succeeded(
            acquired_article.render.id,
            output_blob_id=legacy_article_blob_id,
            output_sha256=hashlib.sha256(legacy_article_bytes).hexdigest(),
            output_byte_size=len(legacy_article_bytes),
        )
        await uow.commit()

    legacy_edition_bytes = b"%PDF-legacy-unpinned-edition-render"
    legacy_edition_blob_id = await artifact_store.put_bytes(
        legacy_edition_bytes,
        bucket="edition-renders-output",
        mime_type="application/pdf",
    )
    legacy_edition_created_at = edition_r1.created_at + timedelta(seconds=10)
    legacy_edition = replace(
        edition_r1,
        id=uuid4(),
        template_version=edition_r1.template_version + "-legacy-current-bundle",
        template_sha256=hashlib.sha256(b"legacy edition template").hexdigest(),
        input_hash=hashlib.sha256(b"legacy edition input identity").hexdigest(),
        source_blob_id=None,
        render_data_blob_id=None,
        output_blob_id=None,
        output_sha256=None,
        output_byte_size=None,
        status=TypstRenderStatus.RUNNING,
        error_code=None,
        error_message=None,
        created_at=legacy_edition_created_at,
        updated_at=legacy_edition_created_at,
    )
    async with uow_factory() as uow:
        acquired_edition = await uow.edition_renders.acquire_for_render(
            legacy_edition,
            stale_running_before=datetime.now().astimezone(),
        )
        await uow.edition_renders.mark_succeeded(
            acquired_edition.render.id,
            output_blob_id=legacy_edition_blob_id,
            output_sha256=hashlib.sha256(legacy_edition_bytes).hexdigest(),
            output_byte_size=len(legacy_edition_bytes),
        )
        await uow.commit()

    article_after_bump_response = await _served_article_pdf(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        publication_renderer=publication_renderer,
        subject_id=artifact.subject_id,
    )
    edition_after_bump = await edition_renderer.render_pdf(release.id)
    served_edition_after_bump = await edition_publication.read_release_pdf(edition.id)
    assert article_after_bump_response.status_code == 200
    assert article_after_bump_response.content == article_r1_bytes
    assert (
        article_after_bump_response.headers["x-publication-render-identity"]
        == article_r1.input_hash
    )
    assert await artifact_store.read_bytes(article_r1.output_blob_id) == article_r1_bytes
    assert edition_after_bump.id == edition_r1.id
    assert edition_after_bump.output_sha256 == edition_r1_sha256
    assert hashlib.sha256(served_edition_after_bump.content).hexdigest() == edition_r1_sha256
    assert served_edition_after_bump.content != legacy_edition_bytes
    assert await artifact_store.read_bytes(r1_manifest_blob_id) == r1_manifest_bytes
    async with uow_factory() as uow:
        assert await uow.publication_manifests.get_blob_id(manifest.id) == r1_manifest_blob_id
        assert (
            await uow._session.scalar(
                select(func.count())
                .select_from(PublicationRenderRow)
                .where(PublicationRenderRow.publication_artifact_id == artifact.id)
            )
            == 2
        )
        assert (
            await uow._session.scalar(
                select(func.count())
                .select_from(EditionRenderRow)
                .where(EditionRenderRow.edition_release_id == release.id)
            )
            == 2
        )
        assert (
            await uow.publication_renders.get_latest_for_artifact(artifact.id)
        ).id == legacy_article.id
        assert (
            await uow.edition_renders.get_latest_for_release(release.id)
        ).id == legacy_edition.id

    # A repin appends a new artifact, manifest and release. It does not mutate R1.
    repinned_artifact = replace(
        artifact,
        id=uuid4(),
        version=2,
        input_hash=hashlib.sha256(f"repin-{uuid4()}".encode()).hexdigest(),
    )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(repinned_artifact)
        await uow.commit()
    release_r2, manifest_r2, _r2_manifest_sha256 = await _persist_release_for_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        edition=edition,
        run=run,
        artifact=repinned_artifact,
        document=document,
        created_at=manifest.created_at + timedelta(seconds=1),
    )
    article_r2_response = await _served_article_pdf(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        publication_renderer=publication_renderer,
        subject_id=repinned_artifact.subject_id,
    )
    assert article_r2_response.status_code == 200
    assert article_r2_response.headers["x-publication-artifact-version"] == "2"
    async with uow_factory() as uow:
        article_r2 = await uow.publication_renders.get_pinned_succeeded_for_artifact(
            repinned_artifact.id
        )
    assert article_r2 is not None
    edition_r2 = await edition_renderer.render_pdf(release_r2.id)
    served_edition_r2 = await edition_publication.read_release_pdf(edition.id)
    assert article_r2.id != article_r1.id
    assert edition_r2.id != edition_r1.id
    assert article_r2.output_blob_id is not None
    assert edition_r2.output_blob_id is not None
    assert article_r2_response.content == await artifact_store.read_bytes(article_r2.output_blob_id)
    assert article_r2.template_version.endswith("-explicit-repin-test")
    assert edition_r2.template_version.endswith("-explicit-repin-test")
    assert article_r2.output_sha256 != article_r1_sha256
    assert edition_r2.output_sha256 != edition_r1_sha256
    assert hashlib.sha256(served_edition_r2.content).hexdigest() == edition_r2.output_sha256
    assert await artifact_store.read_bytes(r1_manifest_blob_id) == r1_manifest_bytes
    assert hashlib.sha256(r1_manifest_bytes).hexdigest() == recorded_manifest_sha256
    assert (await artifact_store.read_bytes(article_r1.output_blob_id)) == article_r1_bytes
    assert (await artifact_store.read_bytes(edition_r1.output_blob_id)) == served_edition_r1.content
    async with uow_factory() as uow:
        assert await uow.edition_releases.get(release.id) == release
        assert await uow.publication_manifests.get(manifest.id) == manifest
        assert await uow.publication_manifests.get(manifest_r2.id) == manifest_r2
        assert (
            await uow._session.scalar(
                select(func.count())
                .select_from(PublicationRenderRow)
                .where(
                    PublicationRenderRow.publication_artifact_id.in_(
                        (artifact.id, repinned_artifact.id)
                    )
                )
            )
            == 3
        )
        assert (
            await uow._session.scalar(
                select(func.count())
                .select_from(EditionRenderRow)
                .where(EditionRenderRow.edition_release_id.in_((release.id, release_r2.id)))
            )
            == 3
        )

"""PostgreSQL/blob-store coverage for PublicationRenderService orchestration."""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from cti_app.application import publication_rendering
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    TypstCompileFailedError,
    TypstCompileRequest,
)
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    publication_document_v4_to_json,
)
from cti_app.domain.publication_render import PublicationRenderStatus
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine
from tests.test_publication_v4 import _diagram
from tests.test_publication_v4 import _document as fixture_document

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CHP_TYPST_ROOT = REPO_ROOT / "chpTypst"
TYPST_FONT_PATHS = (
    CHP_TYPST_ROOT / "HankenGrotesk",
    CHP_TYPST_ROOT / "CascadiaCode" / "ttf",
)


class ControlledTypstCompiler:
    def __init__(self, failures: tuple[Exception, ...] = ()) -> None:
        self.failures = list(failures)
        self.call_count = 0
        self.content = b"%PDF-integration-publication-render"

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.call_count += 1
        assert (request.workspace_root / request.entrypoint_relative_path).is_file()
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
        title="PostgreSQL render fixture",
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
    compiler: ControlledTypstCompiler,
) -> publication_rendering.PublicationRenderService:
    return publication_rendering.PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=MediaAssetStore(artifact_store, uow_factory),
        renderer=TypstRenderer(CHP_TYPST_ROOT),
        compiler=compiler,
        chp_typst_root=CHP_TYPST_ROOT,
        font_paths=TYPST_FONT_PATHS,
        typst_fonts_lock_path=REPO_ROOT / "infra" / "typst-fonts.lock",
    )


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


@pytest.mark.asyncio
async def test_publication_render_postgres_success_idempotency_and_template_identity(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    run, artifact, _ = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
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
    template_values = iter(
        (
            ("integration-template-v1", "a" * 64),
            ("integration-template-v1", "a" * 64),
            ("integration-template-v2", "b" * 64),
        )
    )
    monkeypatch.setattr(
        publication_rendering,
        "compute_template_bundle_hash",
        lambda _: next(template_values),
    )

    first = await service.render_pdf(artifact.id)
    repeated = await service.render_pdf(artifact.id)

    assert first.status is PublicationRenderStatus.SUCCEEDED
    assert first.source_blob_id is not None
    assert first.render_data_blob_id is not None
    assert first.output_blob_id is not None
    assert await artifact_store.read_bytes(first.source_blob_id)
    assert await artifact_store.read_bytes(first.render_data_blob_id)
    assert await artifact_store.read_bytes(first.output_blob_id) == compiler.content
    assert repeated == first
    assert compiler.call_count == 1

    changed_template = await service.render_pdf(artifact.id)

    assert changed_template.id != first.id
    assert changed_template.input_hash != first.input_hash
    assert changed_template.publication_artifact_id == first.publication_artifact_id == artifact.id
    assert compiler.call_count == 2
    async with uow_factory() as uow:
        assert await uow.production_artifacts.get(artifact.id) == before_artifact
        assert await uow.production_runs.get(run.id) == before_run
    assert await _render_status_rows(migrated_postgres_url, artifact.id) == [
        ("succeeded", None),
        ("succeeded", None),
    ]


@pytest.mark.asyncio
async def test_publication_render_postgres_retries_failed_input_in_same_row(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    _, artifact, _ = await _seed_verified_publication(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    monkeypatch.setattr(
        publication_rendering,
        "compute_template_bundle_hash",
        lambda _: ("retry-template", "c" * 64),
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

    assert retried.status is PublicationRenderStatus.SUCCEEDED
    assert retried.error_code is None
    assert compiler.call_count == 2
    assert await _render_status_rows(migrated_postgres_url, artifact.id) == [("succeeded", None)]


@pytest.mark.asyncio
async def test_publication_render_postgres_missing_media_only_fails_render(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(
        publication_rendering,
        "compute_template_bundle_hash",
        lambda _: ("media-failure-template", "d" * 64),
    )
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

"""PostgreSQL end-to-end proof for ordered multi-batch edition publication."""

from __future__ import annotations

import calendar
import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
)
from cti_app.application.edition_rendering import EditionRenderService
from cti_app.application.edition_review import EditionReviewService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import CompiledTypstDocument, TypstCompileRequest
from cti_app.domain.classification import TLP
from cti_app.domain.edition_composition import CompositionDisposition, EditionCompositionEntry
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
    ProductionRunStatus,
    ProductionStage,
    production_batch_request_fingerprint,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    serialize_publication_document,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.jobs import JobRow
from cti_app.infrastructure.database.models.model_execution import ModelRunRow
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CHP_TYPST_ROOT = REPO_ROOT / "chpTypst"
FONT_BUNDLE_ROOT = Path(os.environ.get("FONT_BUNDLE_ROOT") or CHP_TYPST_ROOT)


class _FakeEditionCompiler:
    def __init__(self) -> None:
        self.calls: list[TypstCompileRequest] = []
        self.pdf = b"%PDF-1.7\nmultibatch-edition-test\n"

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.calls.append(request)
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=self.pdf,
            sha256=hashlib.sha256(self.pdf).hexdigest(),
            byte_size=len(self.pdf),
            compiler="typst",
            compiler_version="0.15.1",
        )


async def _compose_three_runs(
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
    *,
    production_order: tuple[str, str, str],
    composition_order: tuple[str, str, str],
) -> tuple[Edition, ProductionArtifactStore, dict[str, Subject], dict[str, UUID]]:
    token = uuid4().int
    year = 2000 + token % 6000
    month = (token >> 12) % 12 + 1
    edition = Edition(
        country="Multi-batch integration",
        country_code=reserve_edition_code(),
        period_start=date(year, month, 1),
        period_end=date(year, month, calendar.monthrange(year, month)[1]),
        tlp=TLP.GREEN,
        languages=("fr",),
    )
    subjects = {
        label: Subject(
            edition_id=edition.id,
            title=f"Article {label}",
            slug=f"multibatch-{label.lower()}-{uuid4().hex}",
            tlp=TLP.GREEN,
        )
        for label in ("A", "B", "C")
    }
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / f"blobs-{uuid4().hex}"), uow_factory)
    )
    now = datetime.now(UTC)
    batches: dict[str, UUID] = {}
    runs: dict[str, ProductionRun] = {}
    artifacts: dict[str, ProductionArtifact] = {}
    batch_items: list[EditionProductionBatchItem] = []
    for production_position, label in enumerate(production_order, start=1):
        subject = subjects[label]
        batch = EditionProductionBatch(
            edition_id=edition.id,
            status=ProductionBatchStatus.COMPLETED,
            phase=ProductionBatchPhase.REVIEW,
            idempotency_key=f"multibatch-{edition.id}-{label}",
            request_fingerprint=production_batch_request_fingerprint(edition.id, [subject.id]),
            actor_id="multibatch-test",
            correlation_id=f"multibatch-{label}",
            created_at=now + timedelta(seconds=production_position),
        )
        run = ProductionRun(
            subject_id=subject.id,
            edition_id=edition.id,
            status=ProductionRunStatus.READY,
            current_stage=ProductionStage.ASSEMBLY,
            run_number=1,
            pipeline_generation=1,
            created_at=now + timedelta(seconds=production_position),
            updated_at=now + timedelta(seconds=production_position),
        )
        document = PublicationDocumentV4(
            schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
            subject_id=subject.id,
            publication_language="fr",
            title=f"Article {label}",
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
        blob_id, _document_sha256 = await artifact_store.put_canonical_json(
            serialize_publication_document(document), bucket="multibatch-publications"
        )
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=1,
            input_hash=hashlib.sha256(f"input-{label}-{uuid4()}".encode()).hexdigest(),
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=blob_id,
        )
        batches[label] = batch.id
        runs[label] = run
        artifacts[label] = artifact
        batch_items.append(
            EditionProductionBatchItem(
                batch_id=batch.id,
                subject_id=subject.id,
                production_run_id=run.id,
                position=1,
                created_at=batch.created_at,
            )
        )
        async with uow_factory() as uow:
            if production_position == 1:
                assert await uow.editions.add_if_absent(edition)
                for persisted_subject in subjects.values():
                    await uow.subjects.add(persisted_subject)
            await uow.production_runs.add(run)
            await uow.production_artifacts.append(artifact)
            await uow.edition_production_batches.add(batch)
            await uow.commit()
            await uow.edition_production_batch_items.append_many((batch_items[-1],))
            await uow.commit()

    entries: list[EditionCompositionEntry] = []
    for position, label in enumerate(composition_order, start=1):
        artifact = artifacts[label]
        assert artifact.canonical_blob_id is not None
        async with uow_factory() as uow:
            blob = await uow.blobs.get(artifact.canonical_blob_id)
        assert blob is not None
        entries.append(
            EditionCompositionEntry(
                subject_id=subjects[label].id,
                disposition=CompositionDisposition.INCLUDED,
                position=position,
                production_run_id=runs[label].id,
                pipeline_generation=runs[label].pipeline_generation,
                artifact_id=artifact.id,
                artifact_version=artifact.version,
                artifact_input_hash=artifact.input_hash,
                document_sha256=blob.descriptor.sha256,
                provenance_batch_id=batches[label],
            )
        )
    composition = await EditionCompositionService(uow_factory).replace_entries(
        edition.id,
        tuple(entries),
        expected_version=0,
        idempotency_key=f"compose-{edition.id}",
        actor_id="editor",
        correlation_id=f"compose-{edition.id}",
    )
    assert composition.version == 1
    return edition, artifact_store, subjects, batches


async def _render_release_pdf(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    tmp_path: Path,
    edition_release_id: UUID,
) -> _FakeEditionCompiler:
    compiler = _FakeEditionCompiler()
    renderer = EditionRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=MediaAssetStore(artifact_store, uow_factory),
        renderer=EditionTypstRenderer(),
        compiler=compiler,
        chp_typst_root=CHP_TYPST_ROOT,
        font_bundle_root=FONT_BUNDLE_ROOT,
        typst_fonts_lock_path=REPO_ROOT / "infra" / "typst-fonts.lock",
    )
    result = await renderer.render_pdf(edition_release_id)
    assert result.status.value == "succeeded"
    assert compiler.calls
    return compiler


async def _assert_manifest_and_release_order(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    edition: Edition,
    expected_labels: tuple[str, str, str],
    subjects: dict[str, Subject],
    batches: dict[str, UUID],
) -> None:
    publication = EditionPublicationService(uow_factory, artifact_store)
    async with uow_factory() as uow:
        job_count_before = await uow._session.scalar(select(func.count()).select_from(JobRow))
        model_run_count_before = await uow._session.scalar(
            select(func.count()).select_from(ModelRunRow)
        )
    review = await EditionReviewService(uow_factory).get(edition.id)
    assert review.can_accept is True
    assert review.composition_version == 1
    accepted = await publication.accept(
        edition.id,
        actor_id="editor",
        expected_composition_version=1,
        correlation_id=f"accept-{edition.id}",
    )
    assert accepted.manifest.schema_version == "2"
    assert [entry.subject_id for entry in accepted.manifest.entries] == [
        subjects[label].id for label in expected_labels
    ]
    assert [entry.position for entry in accepted.manifest.entries] == [1, 2, 3]
    assert [entry.provenance_batch_id for entry in accepted.manifest.entries] == [
        batches[label] for label in expected_labels
    ]
    assert len({entry.production_run_id for entry in accepted.manifest.entries}) == 3
    assert len({entry.document_artifact_id for entry in accepted.manifest.entries}) == 3
    assert len({entry.subject_id for entry in accepted.manifest.entries}) == 3
    release = await EditionAssemblyService(uow_factory, artifact_store).assemble(
        accepted.manifest.id
    )
    payload = await artifact_store.read_json(release.edition_document_blob_id)
    assert [article["document"]["title"] for article in payload["publications"]] == [
        f"Article {label}" for label in expected_labels
    ]
    async with uow_factory() as uow:
        runs = await uow.production_runs.list_for_edition(edition.id)
        assert len(runs) == 3
        assert all(run.status is ProductionRunStatus.READY for run in runs)
        job_count = await uow._session.scalar(select(func.count()).select_from(JobRow))
        model_run_count = await uow._session.scalar(select(func.count()).select_from(ModelRunRow))
        production_jobs = [
            await uow.jobs.list_for_aggregate("production_run", run.id) for run in runs
        ]
    assert not accepted.job_dispatched
    assert job_count == job_count_before
    assert model_run_count == model_run_count_before
    assert production_jobs == [[], [], []]


@pytest.mark.asyncio
async def test_multibatch_abc_publication_freezes_and_renders_ordered_release(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, artifact_store, subjects, batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "B", "C"),
        composition_order=("A", "B", "C"),
    )
    publication = EditionPublicationService(uow_factory, artifact_store)
    await _assert_manifest_and_release_order(
        uow_factory, artifact_store, edition, ("A", "B", "C"), subjects, batches
    )
    status = await publication.release_status(edition.id)
    assert status.release is not None
    compiler = await _render_release_pdf(uow_factory, artifact_store, tmp_path, status.release.id)
    pdf = await publication.read_release_pdf(edition.id)
    assert pdf.content == compiler.pdf
    assert pdf.filename.endswith(".pdf")
    assert (await publication.release_status(edition.id)).pdf_available is True
    async with uow_factory() as uow:
        fixtures = {}
        for label, subject in subjects.items():
            [run] = await uow.production_runs.list_for_subject(subject.id)
            artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.PUBLICATION.value
            )
            assert artifact is not None
            fixtures[label] = {
                "subject_id": str(subject.id),
                "run_id": str(run.id),
                "run_number": run.run_number,
                "batch_id": str(batches[label]),
                "artifact_id": str(artifact.id),
                "artifact_version": artifact.version,
                "artifact_input_hash": artifact.input_hash,
            }
    proof = {
        "scenario": "S1 A/B/C multibatch publication",
        "edition_id": str(edition.id),
        "production_order": ["A", "B", "C"],
        "composition_order": ["A", "B", "C"],
        "fixtures": fixtures,
    }
    proof_path = tmp_path / "multibatch-composition-proof.json"
    proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
    assert proof_path.is_file()


@pytest.mark.asyncio
async def test_multibatch_publication_order_is_independent_of_production_order(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition, artifact_store, subjects, batches = await _compose_three_runs(
        uow_factory,
        tmp_path,
        production_order=("A", "C", "B"),
        composition_order=("B", "A", "C"),
    )
    await _assert_manifest_and_release_order(
        uow_factory, artifact_store, edition, ("B", "A", "C"), subjects, batches
    )

"""PostgreSQL proof that an edition render loads its opinion note-block context.

The edition document embeds an augmented publication whose approved opinion
carries ``ioc_table`` and ``canonical_code`` blocks. The render service must
read exactly the canonical observations and blobs those blocks cite from
PostgreSQL/MinIO, project them through the shared Typst model and refuse a
stale canonical-code reference with its typed B5a code.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.edition_document import edition_metadata_projection
from cti_app.application.edition_rendering import (
    EditionRenderDocumentInvalidError,
    EditionRenderService,
)
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import (
    OpinionCitationContext,
    augment_publication_with_opinion,
)
from cti_app.application.typst_compilation import (
    CompiledTypstDocument,
    TypstCompileRequest,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    CanonicalCodeNoteBlockV1,
    IocTableNoteBlockV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    note_ioc_table_title,
    opinion_blocks_with_observations,
)
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionPublicationV2,
    EditionRelease,
    PublicationManifestV1,
)
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation
from cti_app.domain.production import (
    AnalystInvestigation,
    EditionProductionBatch,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchStatus,
    ProductionRun,
    ProseAuthority,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_v5_sha256,
)
from cti_app.domain.typst_render import TypstRenderStatus
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code
from tests.test_publication_document_v6 import _base_v5

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CHP_TYPST_ROOT = REPO_ROOT / "chpTypst"
FONT_BUNDLE_ROOT = Path(os.environ.get("FONT_BUNDLE_ROOT") or CHP_TYPST_ROOT)

_LEAD_ANCHOR = "lead:0001"
_CODE_BYTES = b"xxCreateFileWyy"
_CODE_EXCERPT = _CODE_BYTES[2:13]


class _FakeEditionCompiler:
    def __init__(self) -> None:
        self.call_count = 0

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.call_count += 1
        assert (request.workspace_root / request.entrypoint_relative_path).is_file()
        content = f"%PDF-edition-note-blocks-{uuid4().hex}".encode()
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            compiler="typst",
            compiler_version="0.15.1",
        )


def _observation(
    *,
    investigation_id: UUID,
    blob_id: UUID,
    sha256: str,
    kind: EvidenceKind,
    source: str,
    object_type: str,
    object_id: str,
    observed_at: datetime,
) -> EvidenceObservation:
    return EvidenceObservation(
        investigation_id=investigation_id,
        kind=kind,
        blob_id=blob_id,
        sha256=sha256,
        source=source,
        source_version="fixture.v1",
        observed_at=observed_at,
        object_type=object_type,
        object_id=object_id,
    )


def _note_opinion(
    base: PublicationDocumentV5,
    *,
    investigation_id: UUID,
    code_observation: EvidenceObservation,
    ioc_observation: EvidenceObservation,
    stale_code_reference: bool,
) -> AnalystOpinionV1:
    code_reference = uuid4() if stale_code_reference else code_observation.id
    note = AnalystObservationV1(
        id=uuid4(),
        anchor=_LEAD_ANCHOR,
        text="NOTE-EDITION-POSTGRES-FIXTURE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            CanonicalCodeNoteBlockV1(
                observation_id=code_reference,
                blob_sha256=code_observation.sha256,
                offset=2,
                length=len(_CODE_EXCERPT),
                excerpt_sha256=hashlib.sha256(_CODE_EXCERPT).hexdigest(),
                language="c",
            ),
            IocTableNoteBlockV1(
                observation_ids=(ioc_observation.id,),
                title=note_ioc_table_title(1),
            ),
        ),
    )
    return AnalystOpinionV1(
        id=uuid4(),
        investigation_id=investigation_id,
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="2" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=publication_document_v5_sha256(base),
        blocks=opinion_blocks_with_observations(
            (
                OpinionBlock(
                    OpinionBlockType.KEY_FINDINGS,
                    "Constat de démonstration.",
                    qualification=OpinionClaimQualification.HYPOTHESIS,
                ),
            ),
            (note,),
        ),
        author_id="fixture-analyst",
        reviewer_id="fixture-reviewer",
        status=AnalystOpinionStatus.APPROVED,
    )


async def _persist_release(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    *,
    stale_code_reference: bool = False,
) -> tuple[EditionRelease, bytes]:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Edition note blocks {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    batch = EditionProductionBatch(
        edition_id=edition.id,
        status=ProductionBatchStatus.QUEUED,
        idempotency_key=f"edition-note-blocks-{suffix}",
    )
    subject = Subject(
        id=uuid4(),
        edition_id=edition.id,
        title="Edition note block subject",
        slug=f"edition-note-blocks-{suffix}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    synthesis_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=hashlib.sha256(b"synthesis").hexdigest(),
        canonical_blob_id=await artifact_store.put_bytes(
            b"synthesis", bucket="edition-note-block-synthesis", mime_type="text/plain"
        ),
        status=ProductionArtifactStatus.VERIFIED,
    )
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=synthesis_artifact.id,
        budget=LoopBudget(),
        prose_authority=ProseAuthority.BRIDGE_ONLY,
    )
    code_blob_id = await artifact_store.put_bytes(
        _CODE_BYTES, bucket="edition-note-block-code", mime_type="application/octet-stream"
    )
    ioc_blob_id = await artifact_store.put_bytes(
        b"shodan observation", bucket="edition-note-block-ioc", mime_type="text/plain"
    )
    observed_at = datetime(2026, 11, 1, tzinfo=UTC)
    code_observation = _observation(
        investigation_id=investigation.id,
        blob_id=code_blob_id,
        sha256=hashlib.sha256(_CODE_BYTES).hexdigest(),
        kind=EvidenceKind.BINARY_ANALYSIS,
        source="native_analysis",
        object_type="file",
        object_id="note-code-sample.bin",
        observed_at=observed_at,
    )
    ioc_observation = _observation(
        investigation_id=investigation.id,
        blob_id=ioc_blob_id,
        sha256=hashlib.sha256(b"shodan observation").hexdigest(),
        kind=EvidenceKind.SHODAN_OBSERVATION,
        source="shodan",
        object_type="ip",
        object_id="192.0.2.10",
        observed_at=observed_at,
    )
    base_v5 = _base_v5()
    base = replace(base_v5, document=replace(base_v5.document, subject_id=subject.id))
    opinion = _note_opinion(
        base,
        investigation_id=investigation.id,
        code_observation=code_observation,
        ioc_observation=ioc_observation,
        stale_code_reference=stale_code_reference,
    )
    publication = augment_publication_with_opinion(
        base,
        opinion,
        OpinionCitationContext(
            base_publication_artifact_id=opinion.base_publication_artifact_id,
            base_publication_sha256=opinion.base_publication_sha256,
            investigation_id=investigation.id,
            approver_id="fixture-approver",
            findings=(),
            observations=(code_observation, ioc_observation),
            tlp_violation=False,
            do_not_submit=False,
        ),
    )
    document = EditionDocumentV2(
        edition=edition_metadata_projection(edition),
        publications=(
            EditionPublicationV2(
                position=1,
                subject_id=subject.id,
                document=publication,
            ),
        ),
    )
    document_blob_id, document_sha256 = await artifact_store.put_canonical_json(
        document.to_json(), bucket="edition-note-block-documents"
    )
    manifest = PublicationManifestV1.create(
        edition_id=edition.id,
        edition_version=edition.version,
        batch_id=batch.id,
        created_by="edition-note-blocks-postgres-test",
        entries=(),
        exclusions=(),
    )
    manifest_blob_id, _ = await artifact_store.put_canonical_json(
        manifest.to_json(), bucket="edition-note-block-manifests"
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
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis_artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.evidence_observations.append_if_absent(code_observation)
        await uow.evidence_observations.append_if_absent(ioc_observation)
        await uow.commit()
    async with uow_factory() as uow:
        await uow.publication_manifests.add(manifest, manifest_blob_id)
        assert await uow.edition_releases.add_if_absent(release)
        await uow.commit()
    return release, _CODE_EXCERPT


def _render_service(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    compiler: _FakeEditionCompiler,
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


async def _postgres_fixture(migrated_postgres_url: str, tmp_path: Path):
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    blob_store = FilesystemBlobStore(tmp_path / f"edition-note-blocks-{uuid4().hex}")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    return engine, uow_factory, artifact_store


@pytest.mark.asyncio
async def test_edition_render_loads_note_block_context_from_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine, uow_factory, artifact_store = await _postgres_fixture(migrated_postgres_url, tmp_path)
    try:
        release, excerpt = await _persist_release(uow_factory, artifact_store)
        compiler = _FakeEditionCompiler()
        service = _render_service(uow_factory, artifact_store, compiler)

        render = await service.render_pdf(release.id)

        assert render.status is TypstRenderStatus.SUCCEEDED
        assert render.render_data_blob_id is not None
        assert compiler.call_count == 1
        render_data = json.loads(await artifact_store.read_bytes(render.render_data_blob_id))
        synthesis = next(
            section
            for section in render_data["publications"][0]["content_sections"]
            if section["type"] == "synthesis"
        )
        note = next(
            block
            for block in synthesis["blocks"]
            if block.get("type") == "analyst_observation" and block.get("blocks")
        )
        code_block, table_block = note["blocks"]
        assert [block["block_type"] for block in note["blocks"]] == [
            "canonical_code",
            "ioc_table",
        ]
        assert code_block["excerpt"] == excerpt.decode("utf-8")
        code_provenance = "".join(part["text"] for part in code_block["provenance"])
        assert hashlib.sha256(_CODE_BYTES).hexdigest() in code_provenance
        assert hashlib.sha256(excerpt).hexdigest() in code_provenance
        (table_row,) = table_block["pipe_table"]["rows"]
        assert [cell["text"] for cell in table_row][1:] == [
            "ip",
            "192.0.2.10",
            "shodan",
            datetime(2026, 11, 1, tzinfo=UTC).isoformat(),
            hashlib.sha256(b"shodan observation").hexdigest(),
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_edition_render_refuses_stale_note_code_reference(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine, uow_factory, artifact_store = await _postgres_fixture(migrated_postgres_url, tmp_path)
    try:
        release, _excerpt = await _persist_release(
            uow_factory, artifact_store, stale_code_reference=True
        )
        compiler = _FakeEditionCompiler()
        service = _render_service(uow_factory, artifact_store, compiler)

        with pytest.raises(EditionRenderDocumentInvalidError) as raised:
            await service.render_pdf(release.id)

        assert raised.value.code == "edition_render_document_invalid"
        assert "note_code_ref_stale" in str(raised.value)
        assert compiler.call_count == 0
        async with uow_factory() as uow:
            failed = await uow.edition_renders.get_latest_for_release(release.id)
        assert failed is not None
        assert failed.status is TypstRenderStatus.FAILED
        assert failed.error_code == "edition_render_document_invalid"
    finally:
        await engine.dispose()

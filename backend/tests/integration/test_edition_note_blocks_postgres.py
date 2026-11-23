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
from cti_app.application.publication_builder import (
    build_article_format_v1,
    build_publication_document_v5,
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
    ArticleElementNoteBlockV1,
    CanonicalCodeNoteBlockV1,
    IocTableNoteBlockV1,
    NoteArticleElementKind,
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
from cti_app.domain.production_editorial_enrichment import (
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import extraction_evidence_refs_v1
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationDocumentV7,
    PublicationDocumentV9,
    ResolvedPublicationPlacementV1,
    publication_document_v5_sha256,
    publication_element_sha256,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.typst_render import TypstRenderStatus
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.integration.edition_codes import reserve_edition_code
from tests.test_publication_builder_v4 import _canonical_inputs
from tests.test_publication_builder_v4 import (
    _table as _enrichment_table,
)
from tests.test_publication_document_v6 import _base_v5
from tests.test_publication_document_v7 import _v6

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
    base: PublicationDocumentV5 | PublicationDocumentV9,
    *,
    investigation_id: UUID,
    code_observation: EvidenceObservation,
    ioc_observation: EvidenceObservation,
    stale_code_reference: bool,
    article_element_block: ArticleElementNoteBlockV1 | None = None,
    base_sha256: str | None = None,
) -> AnalystOpinionV1:
    code_reference = uuid4() if stale_code_reference else code_observation.id
    content_blocks: tuple = (
        *((article_element_block,) if article_element_block is not None else ()),
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
    )
    note = AnalystObservationV1(
        id=uuid4(),
        anchor=_LEAD_ANCHOR,
        text="NOTE-EDITION-POSTGRES-FIXTURE",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=content_blocks,
    )
    return AnalystOpinionV1(
        id=uuid4(),
        investigation_id=investigation_id,
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="2" * 64,
        base_publication_artifact_id=uuid4(),
        base_publication_sha256=(
            base_sha256 if base_sha256 is not None else publication_document_v5_sha256(base)  # type: ignore[arg-type]
        ),
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


_V9_TABLE_ANCHOR_ID = UUID(int=7701)
_V9_TABLE_KEY = "edition-note-linked-table"


def _v9_publication(subject_id: UUID) -> tuple[PublicationDocumentV9, PublicationDocumentV5]:
    """Build a V9 article whose after-paragraph table resolves to lead:0001."""
    snapshot, references, extraction, synthesis = _canonical_inputs()
    table = replace(
        _enrichment_table(extraction_evidence_refs_v1(extraction)[0], key=_V9_TABLE_KEY),
        placement=EnrichmentPlacementV1(
            kind=EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=_V9_TABLE_ANCHOR_ID,
        ),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(table,),
    )
    base_v5 = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    base_v5 = replace(base_v5, document=replace(base_v5.document, subject_id=subject_id))
    v9 = PublicationDocumentV9(
        base_publication=PublicationDocumentV7(
            base_publication=_v6(base_v5),
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="a" * 64,
            routing_policy_version="routing-policy-v1",
        ),
        article_format=build_article_format_v1(base_v5, extraction),
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=(
            ResolvedPublicationPlacementV1(
                element_kind="table",
                element_key=table.key,
                element_position=0,
                anchor_block_id=_V9_TABLE_ANCHOR_ID,
                anchor_pointer="/lead/0",
                anchor_canonical_position=0,
                paragraph_kind="lead",
                section_index=None,
                paragraph_index=0,
            ),
        ),
    )
    return v9, base_v5


async def _persist_release(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    *,
    stale_code_reference: bool = False,
    v9_base: bool = False,
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
    if v9_base:
        base_document, resolved_base = _v9_publication(subject.id)
        published_table = resolved_base.document.tables[0]
        article_element_block: ArticleElementNoteBlockV1 | None = ArticleElementNoteBlockV1(
            kind=NoteArticleElementKind.TABLE,
            element_key=published_table.key,
            element_sha256=publication_element_sha256(
                resolved_base.document, kind="table", key=published_table.key
            ),
            anchor=_LEAD_ANCHOR,
        )
        base_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(
                serialize_publication_document(base_document)
            )
        ).hexdigest()
    else:
        base_document = base
        article_element_block = None
        base_sha256 = None
    opinion = _note_opinion(
        base_document,
        investigation_id=investigation.id,
        code_observation=code_observation,
        ioc_observation=ioc_observation,
        stale_code_reference=stale_code_reference,
        article_element_block=article_element_block,
        base_sha256=base_sha256,
    )
    publication = augment_publication_with_opinion(
        base_document,
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
    if v9_base:
        assert type(publication) is PublicationDocumentV9
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
async def test_edition_render_renders_note_article_element_from_resolved_v9_base(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    """R3-F1: an article_element note on a V9 base reaches the edition render."""
    engine, uow_factory, artifact_store = await _postgres_fixture(migrated_postgres_url, tmp_path)
    try:
        release, excerpt = await _persist_release(uow_factory, artifact_store, v9_base=True)
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
        assert [block["block_type"] for block in note["blocks"]] == [
            "article_element",
            "canonical_code",
            "ioc_table",
        ]
        element = note["blocks"][0]
        assert element["kind"] == "table"
        assert element["element_key"] == _V9_TABLE_KEY
        assert element["anchor"] == _LEAD_ANCHOR
        assert element["reference_label"] == "Tableau — Observed commands"
        element_provenance = "".join(part["text"] for part in element["provenance"])
        assert element["element_sha256"] in element_provenance
        assert _LEAD_ANCHOR in element_provenance
        # The element is rendered once at its article placement; the note only
        # carries the deterministic cross-reference.
        rendered_elements = [
            block
            for block in synthesis["blocks"]
            if block.get("type") == "table" and block.get("key") == _V9_TABLE_KEY
        ]
        assert len(rendered_elements) == 1
        assert synthesis["blocks"].index(rendered_elements[0]) < synthesis["blocks"].index(note)
        # The other note blocks still read their canonical PG observations.
        assert note["blocks"][1]["excerpt"] == excerpt.decode("utf-8")
        assert [cell["text"] for cell in note["blocks"][2]["pipe_table"]["rows"][0]][1:] == [
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

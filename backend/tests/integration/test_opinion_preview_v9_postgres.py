"""R6-F1: the opinion preview endpoint renders a V9/V10-based draft end to end.

The preview projects the exact V5 body of the pinned V9 envelope, so its
after-paragraph elements, anchored notes and cross-references must match the
publication instead of being refused as ``editorial_anchor_stale``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pypdf import PdfReader

from cti_app.api.analyst_opinion import router as analyst_opinion_router
from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    ArticleElementNoteBlockV1,
    IocTableNoteBlockV1,
    NoteArticleElementKind,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    note_ioc_table_title,
    opinion_blocks_with_observations,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_document import (
    publication_element_sha256,
    serialize_publication_document,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.edition_codes import reserve_edition_code
from tests.test_publication_builder_v4 import _canonical_inputs
from tests.test_typst_rendering import _NOTE_BLOCK_ANCHOR, _after_paragraph_table, _note_block_v9

pytestmark = pytest.mark.integration

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class _PostgresArtifactStore:
    def __init__(self, uow_factory, blobs: dict[UUID, bytes]) -> None:
        self._uow_factory = uow_factory
        self.blobs = blobs

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        content = self.blobs[blob_id]
        if max_bytes is not None and len(content) > max_bytes:
            raise ValueError("Blob exceeds the requested read limit")
        return content

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        value = json.loads(await self.read_bytes(blob_id))
        assert isinstance(value, dict)
        return value

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        record = BlobRecord(
            descriptor=BlobDescriptor(
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
                mime_type=mime_type,
                logical_bucket=bucket,
            )
        )
        async with self._uow_factory() as uow:
            await uow.blobs.add(record)
            await uow.commit()
        self.blobs[record.id] = content
        return record.id


def _pinned_typst_binary() -> str | None:
    binary = os.environ.get("TYPST_BINARY") or str(
        Path.home() / ".local" / "share" / "autowork-tools" / "typst"
    )
    if shutil.which(binary) is None and not Path(binary).is_file():
        return None
    version = subprocess.run(
        (binary, "--version"), capture_output=True, check=True, text=True, timeout=10
    ).stdout.strip()
    assert version.startswith("typst 0.15.1"), version
    return binary


@pytest.mark.asyncio
async def test_postgres_v9_opinion_preview_renders_real_pdf(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    binary = _pinned_typst_binary()
    if binary is None:
        pytest.skip("pinned Typst 0.15.1 is unavailable")

    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    table = _after_paragraph_table()
    v9, base, _extraction = _note_block_v9(tables=(table,))
    _snapshot, _references, _extraction_inputs, synthesis = _canonical_inputs()
    base_bytes = ProductionArtifactStore.canonical_json_bytes(serialize_publication_document(v9))
    synthesis_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_synthesis_to_json(synthesis)
    )
    observation_bytes = b"harmless integration observation evidence"
    blobs: dict[UUID, bytes] = {}
    store = _PostgresArtifactStore(uow_factory, blobs)
    base_blob_id = await store.put_bytes(
        base_bytes, bucket="opinion-preview-v9-base", mime_type="application/json"
    )
    synthesis_blob_id = await store.put_bytes(
        synthesis_bytes, bucket="opinion-preview-v9-synthesis", mime_type="application/json"
    )
    observation_blob_id = await store.put_bytes(
        observation_bytes, bucket="opinion-preview-v9-observation", mime_type="text/plain"
    )

    suffix = uuid4().hex
    edition = Edition(
        country=f"Opinion preview V9 {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.CLEAR,
        languages=("en",),
    )
    subject = Subject(
        id=base.subject_id,
        edition_id=edition.id,
        title=base.title,
        slug=f"opinion-preview-v9-{suffix}",
        tlp=TLP.CLEAR,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    base_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(base_bytes).hexdigest(),
        canonical_blob_id=base_blob_id,
    )
    synthesis_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=hashlib.sha256(synthesis_bytes).hexdigest(),
        canonical_blob_id=synthesis_blob_id,
    )
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=synthesis_artifact.id,
        budget=LoopBudget(),
    )
    observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=observation_blob_id,
        sha256=hashlib.sha256(observation_bytes).hexdigest(),
        source="fixture-static-analysis",
        source_version="1",
        observed_at=datetime(2026, 11, 20, 10, 0, 0, 123456, tzinfo=UTC),
        object_type="file",
        object_id="preview-sample.bin",
        limitations=("Static observation only.",),
    )
    published_table = base.document.tables[0]
    element_sha256 = publication_element_sha256(
        base.document, kind="table", key=published_table.key
    )
    note = AnalystObservationV1(
        id=UUID(int=8860),
        anchor=_NOTE_BLOCK_ANCHOR,
        text="NOTE-PREVIEW-V9",
        state=AnalystObservationState.UNRESOLVED,
        relation=AnalystObservationRelation.UNRESOLVED,
        content_blocks=(
            ArticleElementNoteBlockV1(
                kind=NoteArticleElementKind.TABLE,
                element_key=published_table.key,
                element_sha256=element_sha256,
                anchor=_NOTE_BLOCK_ANCHOR,
            ),
            IocTableNoteBlockV1(
                observation_ids=(observation.id,),
                title=note_ioc_table_title(1),
            ),
        ),
    )
    opinion = AnalystOpinionV1(
        id=UUID(int=8861),
        investigation_id=investigation.id,
        synthesis_artifact_id=synthesis_artifact.id,
        synthesis_sha256=hashlib.sha256(synthesis_bytes).hexdigest(),
        base_publication_artifact_id=base_artifact.id,
        base_publication_sha256=hashlib.sha256(base_bytes).hexdigest(),
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
        author_id="operator:integration-preview",
        status=AnalystOpinionStatus.DRAFT,
    )
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.subjects.add(subject)
            await uow.production_runs.add(run)
            await uow.production_artifacts.append(synthesis_artifact)
            await uow.production_artifacts.append(base_artifact)
            await uow.analyst_investigations.add(investigation)
            await uow.evidence_observations.append_if_absent(observation)
            await uow.analyst_opinions.append_version(opinion, expected_version=0)
            await uow.commit()

        render_service = PublicationRenderService(
            uow_factory=uow_factory,  # type: ignore[arg-type]
            artifact_store=store,  # type: ignore[arg-type]
            media_asset_store=MediaAssetStore(store, uow_factory),  # type: ignore[arg-type]
            renderer=TypstRenderer(),
            compiler=TypstSubprocessCompiler(binary=binary),
            chp_typst_root=_REPOSITORY_ROOT / "chpTypst",
            font_bundle_root=Path(
                os.environ.get("FONT_BUNDLE_ROOT", str(_REPOSITORY_ROOT / "chpTypst"))
            ),
            typst_fonts_lock_path=_REPOSITORY_ROOT / "infra" / "typst-fonts.lock",
        )
        preview_app = FastAPI()
        preview_app.include_router(analyst_opinion_router)
        preview_app.state.uow_factory = uow_factory
        preview_app.state.production_artifact_store = store
        preview_app.state.analyst_opinion_preview_service = AnalystOpinionPreviewService(
            uow_factory=uow_factory,  # type: ignore[arg-type]
            artifact_store=store,  # type: ignore[arg-type]
            renderer=render_service,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=preview_app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/opinions/{opinion.id}/preview.pdf",
                params={
                    "version": opinion.version,
                    "opinion_sha256": opinion.content_sha256,
                    "base_publication_sha256": opinion.base_publication_sha256,
                },
            )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["x-opinion-status"] == "draft-unapproved"
        assert response.headers["x-base-publication-sha256"] == opinion.base_publication_sha256
    finally:
        await engine.dispose()

    pdf = PdfReader(io.BytesIO(response.content), strict=True)
    assert pdf.pages
    compact_text = "".join("".join((page.extract_text() or "").split()) for page in pdf.pages)
    lead_text = "".join(base.document.lead[0].text.split())
    table_title = "".join(published_table.title.split())
    note_text = "NOTE-PREVIEW-V9"
    cross_reference = "".join(f"Tableau — {published_table.title}".split())
    assert note_text in compact_text
    assert cross_reference in compact_text
    assert compact_text.index(lead_text) < compact_text.index(table_title)
    assert compact_text.index(table_title) < compact_text.index(note_text)
    # The element is rendered once at its resolved placement; the note only
    # carries the deterministic cross-reference.
    assert compact_text.count("-enc") == 1

from __future__ import annotations

import hashlib
from datetime import date
from uuid import uuid4

import pytest

from cti_app.application.analyst_opinion_service import AnalystOpinionService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_text_correction import (
    PublicationTextCorrectionCommand,
    PublicationTextCorrectionService,
)
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.publication_document import serialize_publication_document
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.test_publication_document_v6 import _base_v5

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_correction_and_rebase_are_append_only(uow_factory, tmp_path) -> None:
    document = _base_v5()
    period_start = date(2026, 1, 1)
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=period_start,
        period_end=date(2026, 1, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        id=document.subject_id,
        edition_id=edition.id,
        title="Publication text correction fixture",
        slug=f"publication-correction-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=hashlib.sha256(b"integration synthesis").hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
    )
    canonical_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(document)
    )
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(
            FilesystemBlobStore(tmp_path / "publication-correction-blobs"), uow_factory
        )
    )
    _, canonical_blob_id, _ = await artifact_store.store_stage_payloads(
        canonical=serialize_publication_document(document)
    )
    assert canonical_blob_id is not None
    base = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(b"integration publication inputs").hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_blob_id,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(),
    )
    opinion = AnalystOpinionV1(
        investigation_id=investigation.id,
        synthesis_artifact_id=synthesis.id,
        synthesis_sha256="b" * 64,
        base_publication_artifact_id=base.id,
        base_publication_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        blocks=(
            OpinionBlock(
                block_type=OpinionBlockType.KEY_FINDINGS,
                text="Un point à relire après la correction de la publication.",
                qualification=OpinionClaimQualification.INFERENCE,
                certainty=0.6,
            ),
        ),
        author_id="analyst:integration",
        reviewer_id="editor:integration",
        status=AnalystOpinionStatus.APPROVED,
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis)
        await uow.production_artifacts.append(base)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_opinions.append_version(opinion, expected_version=0)
        await uow.commit()

    first_text = document.lead[0].text
    result = await PublicationTextCorrectionService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    ).correct(
        PublicationTextCorrectionCommand(
            subject_id=subject.id,
            artifact_id=base.id,
            artifact_version=base.version,
            artifact_input_hash=base.input_hash,
            artifact_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
            field_path="lead[0].text",
            expected_text=first_text,
            replacement_text=f"{first_text} Correction de test.",
            actor_id="human:integration-editor",
            reason="Correction exacte pour le test d'intégration.",
            idempotency_key=f"publication-correction-{uuid4().hex}",
        )
    )
    rebased = await AnalystOpinionService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    ).rebase(
        opinion.id,
        opinion.version,
        opinion.content_sha256,
        result.artifact.id,
        result.canonical_sha256,
        "human:integration-reviewer",
    )

    assert result.artifact.version == 2
    assert rebased.opinion.version == opinion.version + 1
    assert rebased.opinion.status is AnalystOpinionStatus.DRAFT
    assert rebased.opinion.blocks == opinion.blocks
    assert rebased.opinion.base_publication_artifact_id == result.artifact.id
    async with uow_factory() as uow:
        old_artifact = await uow.production_artifacts.get(base.id)
        new_opinion = await uow.analyst_opinions.get(opinion.id)
        original_opinion = await uow.analyst_opinions.get(opinion.id, version=opinion.version)
        assert old_artifact == base
        assert new_opinion == rebased.opinion
        assert original_opinion == opinion

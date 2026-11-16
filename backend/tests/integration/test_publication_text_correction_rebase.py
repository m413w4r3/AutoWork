from __future__ import annotations

import hashlib
from datetime import date
from uuid import uuid4

import pytest

from cti_app.application.analyst_opinion_service import AnalystOpinionService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_references import production_reference_corpus_from_json
from cti_app.application.publication_builder import build_publication_document_v5
from cti_app.application.publication_text_correction import (
    PublicationTextCorrectionCommand,
    PublicationTextCorrectionService,
)
from cti_app.application.subject_production import SubjectProductionService
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
    ProductionStage,
)
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_from_json
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_document import serialize_publication_document
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.integration.edition_codes import reserve_edition_code
from tests.integration.test_production_reuse_postgres import (
    _canonical_synthesis,
    _persist_edition,
    _prepare_reusable_article,
    _projection_artifact,
    _seed_subject_discovery_lineage,
    _store_canonical_first_pass,
    _store_empty_enrichment,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_correction_and_rebase_are_append_only(uow_factory, tmp_path) -> None:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Publication correction {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    await _persist_edition(uow_factory, edition)
    subject = Subject(
        edition_id=edition.id,
        title="Publication text correction fixture",
        slug=f"publication-correction-{suffix}",
        tlp=TLP.AMBER,
    )
    async with uow_factory() as uow:
        await uow.subjects.add(subject)
        await uow.commit()
    batch, source = await _prepare_reusable_article(
        uow_factory,
        edition=edition,
        subject=subject,
        title=subject.title,
    )
    await _seed_subject_discovery_lineage(
        uow_factory,
        edition=edition,
        subject_batches=((subject, batch),),
    )

    artifact_store = ProductionArtifactStore(
        BlobCatalogService(
            FilesystemBlobStore(tmp_path / "publication-correction-blobs"), uow_factory
        )
    )
    production = SubjectProductionService(uow_factory)
    run, created = await production.create_run(subject.id, edition.id)
    assert created
    run = await production.start_run(run.id)
    async with uow_factory() as uow:
        persisted = await uow.production_runs.get_for_update(run.id)
        assert persisted is not None
        persisted.current_stage = ProductionStage.ASSEMBLY
        persisted.mark_ready()
        await uow.production_runs.save(persisted)
        await uow.commit()
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
    assert snapshot is not None

    event_text = f"{subject.slug}.security-lab.io was reported."
    first_pass = await _store_canonical_first_pass(
        uow_factory,
        artifact_store,
        snapshot=snapshot,
        subject=subject,
        source=source,
        event_text=event_text,
    )
    synthesis_value = _canonical_synthesis(snapshot, first_pass.extraction, event_text)
    _, synthesis_blob_id, _ = await artifact_store.store_stage_payloads(
        canonical=production_synthesis_to_json(synthesis_value)
    )
    assert synthesis_blob_id is not None
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=first_pass.synthesis_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=synthesis_blob_id,
        metadata={"relevance_projection_hash": first_pass.projection.projection_hash},
    )
    references = production_reference_corpus_from_json(
        await artifact_store.read_json(first_pass.refs_blob_id)
    )
    source_artifacts = {
        ProductionArtifactStage.REFERENCES: ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash=first_pass.refs_hash,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=first_pass.refs_raw_id,
            canonical_blob_id=first_pass.refs_blob_id,
        ),
        ProductionArtifactStage.EXTRACTION: ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash=first_pass.extraction_hash,
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=first_pass.extraction_blob_id,
        ),
        ProductionArtifactStage.SYNTHESIS: synthesis,
        ProductionArtifactStage.RELEVANCE_PROJECTION: await _projection_artifact(
            artifact_store,
            run_id=run.id,
            subject_id=subject.id,
            projection=first_pass.projection,
        ),
    }
    async with uow_factory() as uow:
        for artifact in source_artifacts.values():
            await uow.production_artifacts.append(artifact)
        await uow.commit()
    enrichment_artifact = await _store_empty_enrichment(
        uow_factory,
        artifact_store,
        run_id=run.id,
        subject_id=subject.id,
        snapshot=snapshot,
        extraction=first_pass.extraction,
        synthesis=synthesis_value,
        projection=first_pass.projection,
    )
    source_artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT] = enrichment_artifact
    enrichment = editorial_enrichment_from_json(
        await artifact_store.read_json(enrichment_artifact.canonical_blob_id)
    )
    document = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=first_pass.extraction,
        relevance_projection=first_pass.projection,
        synthesis=synthesis_value,
        editorial_enrichment=enrichment,
    )
    publication_payload = serialize_publication_document(document)
    canonical_bytes = ProductionArtifactStore.canonical_json_bytes(publication_payload)
    _, canonical_blob_id, _ = await artifact_store.store_stage_payloads(
        canonical=publication_payload
    )
    assert canonical_blob_id is not None
    input_artifacts = {
        "references_artifact_id": str(source_artifacts[ProductionArtifactStage.REFERENCES].id),
        "extraction_artifact_id": str(source_artifacts[ProductionArtifactStage.EXTRACTION].id),
        "relevance_projection_artifact_id": str(
            source_artifacts[ProductionArtifactStage.RELEVANCE_PROJECTION].id
        ),
        "synthesis_artifact_id": str(synthesis.id),
        "editorial_enrichment_artifact_id": str(enrichment_artifact.id),
    }
    base = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(canonical_bytes).hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_blob_id,
        metadata={"input_artifacts": input_artifacts},
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(),
    )
    opinion = AnalystOpinionV1(
        investigation_id=investigation.id,
        synthesis_artifact_id=synthesis.id,
        synthesis_sha256=hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(
                production_synthesis_to_json(synthesis_value)
            )
        ).hexdigest(),
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
    assert result.artifact.canonical_blob_id is not None
    corrected_payload = await artifact_store.read_json(result.artifact.canonical_blob_id)
    before_lead = next(
        item
        for item in publication_payload["rich_text"]["paragraphs"]
        if item["anchor"] == "lead:0001"
    )
    after_lead = next(
        item
        for item in corrected_payload["rich_text"]["paragraphs"]
        if item["anchor"] == "lead:0001"
    )
    before_semantic_spans = [span for span in before_lead["spans"] if span["role"] != "text"]
    after_semantic_spans = [span for span in after_lead["spans"] if span["role"] != "text"]
    assert before_semantic_spans
    assert any(span["role"] == "ioc" for span in before_semantic_spans)
    assert after_semantic_spans == before_semantic_spans
    before_text_spans = [span for span in before_lead["spans"] if span["role"] == "text"]
    after_text_spans = [span for span in after_lead["spans"] if span["role"] == "text"]
    assert len(before_text_spans) == len(after_text_spans) == 1
    assert after_text_spans[0]["text"] == f"{before_text_spans[0]['text']} Correction de test."
    async with uow_factory() as uow:
        old_artifact = await uow.production_artifacts.get(base.id)
        new_opinion = await uow.analyst_opinions.get(opinion.id)
        original_opinion = await uow.analyst_opinions.get(opinion.id, version=opinion.version)
        assert old_artifact == base
        assert new_opinion == rebased.opinion
        assert original_opinion == opinion

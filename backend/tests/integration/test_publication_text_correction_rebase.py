from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from cti_app.application.analyst_opinion_service import AnalystOpinionService
from cti_app.application.analyst_publication_service import AnalystPublicationService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_governance import (
    InvestigationEnvelopeProposal,
    InvestigationEnvelopeService,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_references import production_reference_corpus_from_json
from cti_app.application.publication_augmentation import (
    OpinionCitationContext,
    augment_publication_with_opinion,
)
from cti_app.application.publication_builder import (
    build_article_format_v1,
    build_publication_document_v5,
)
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
from cti_app.domain.investigation import DisclosureBounds, InvestigationBudget, TLPLevel
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
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    PublicationDocumentV7,
    PublicationDocumentV8,
    PublicationKind,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SemanticAnnotationProposalV1,
    SemanticRole,
    SemanticTextSpanV1,
)
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
@pytest.mark.parametrize("request_head", [False, True])
async def test_postgres_correction_and_rebase_are_append_only(
    uow_factory, tmp_path, request_head: bool
) -> None:
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
        title="WebSockets Region X",
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

    event_text = (
        f"{subject.slug}.security-lab.io was reported with WebSockets activity "
        "attributed to Region X."
    )
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
        annotations=(
            SemanticAnnotationProposalV1(
                paragraph_anchor="lead:0001",
                role=SemanticRole.TECHNICAL,
                text="WebSockets",
            ),
            SemanticAnnotationProposalV1(
                paragraph_anchor="lead:0001",
                role=SemanticRole.PROPER_NAME,
                text="Region X",
            ),
        ),
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
        version=7,
        input_hash=hashlib.sha256(canonical_bytes).hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_blob_id,
        metadata={"input_artifacts": input_artifacts},
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(),
    )
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(production_synthesis_to_json(synthesis_value))
    ).hexdigest()
    opinion_block = OpinionBlock(
        block_type=OpinionBlockType.KEY_FINDINGS,
        text="Un point à relire après la correction de la publication.",
        qualification=OpinionClaimQualification.INFERENCE,
        certainty=0.6,
    )
    base_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    embedded_opinion = AnalystOpinionV1(
        id=uuid4(),
        investigation_id=investigation.id,
        synthesis_artifact_id=synthesis.id,
        synthesis_sha256=synthesis_sha256,
        base_publication_artifact_id=base.id,
        base_publication_sha256=base_sha256,
        blocks=(opinion_block,),
        author_id="analyst:previous-publication",
        reviewer_id="editor:previous-publication",
        status=AnalystOpinionStatus.APPROVED,
    )
    previous_v6 = augment_publication_with_opinion(
        document,
        embedded_opinion,
        OpinionCitationContext(
            base_publication_artifact_id=base.id,
            base_publication_sha256=base_sha256,
            investigation_id=investigation.id,
            approver_id="human:previous-publication",
            findings=(),
            observations=(),
            tlp_violation=False,
            do_not_submit=False,
        ),
    )
    assert isinstance(previous_v6, PublicationDocumentV6)
    article_format = build_article_format_v1(document, first_pass.extraction)
    requested_document = PublicationDocumentV8(
        base_publication=PublicationDocumentV7(
            base_publication=previous_v6,
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="a" * 64,
            routing_policy_version="routing-policy-v1",
        ),
        article_format=article_format,
    )
    requested_payload = serialize_publication_document(requested_document)
    requested_bytes = ProductionArtifactStore.canonical_json_bytes(requested_payload)
    _, requested_blob_id, _ = await artifact_store.store_stage_payloads(canonical=requested_payload)
    assert requested_blob_id is not None
    requested = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=8,
        input_hash=hashlib.sha256(requested_bytes).hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=requested_blob_id,
        metadata={
            **base.metadata,
            "previous_publication_artifact_id": str(base.id),
        },
    )
    head_document = PublicationDocumentV8(
        base_publication=replace(
            requested_document.base_publication,
            routing_decision_hash="b" * 64,
        ),
        article_format=requested_document.article_format,
    )
    head_payload = serialize_publication_document(head_document)
    head_bytes = ProductionArtifactStore.canonical_json_bytes(head_payload)
    _, head_blob_id, _ = await artifact_store.store_stage_payloads(canonical=head_payload)
    assert head_blob_id is not None
    head = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=9,
        input_hash=hashlib.sha256(head_bytes).hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=head_blob_id,
        metadata={
            **base.metadata,
            "previous_publication_artifact_id": str(requested.id),
        },
    )
    correction_base = head if request_head else requested
    correction_base_bytes = head_bytes if request_head else requested_bytes
    opinion = AnalystOpinionV1(
        investigation_id=investigation.id,
        synthesis_artifact_id=synthesis.id,
        synthesis_sha256=synthesis_sha256,
        base_publication_artifact_id=correction_base.id,
        base_publication_sha256=hashlib.sha256(correction_base_bytes).hexdigest(),
        blocks=(opinion_block,),
        author_id="analyst:integration",
        reviewer_id="editor:integration",
        status=AnalystOpinionStatus.APPROVED,
    )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(base)
        await uow.production_artifacts.append(requested)
        await uow.production_artifacts.append(head)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_opinions.append_version(opinion, expected_version=0)
        await uow.commit()

    await InvestigationEnvelopeService(uow_factory).approve(
        InvestigationEnvelopeProposal(
            investigation_id=investigation.id,
            allowed_providers=(),
            budget=InvestigationBudget(),
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.AMBER,
                do_not_submit=False,
                external_llm_allowed=False,
            ),
        ),
        actor_id="human:integration-g2-reviewer",
        reason="Local integration fixture for the approved opinion workflow.",
        correlation_id=f"publication-correction-envelope-{suffix}",
    )

    before_title = document.title
    before_title_spans = next(
        item["spans"]
        for item in publication_payload["rich_text"]["paragraphs"]
        if item["anchor"] == "title"
    )
    assert any(span["role"] == "technical" for span in before_title_spans)
    assert any(span["role"] == "proper_name" for span in before_title_spans)
    replacement_spans = (
        SemanticTextSpanV1(SemanticRole.TEXT, "[Example publisher] "),
        SemanticTextSpanV1(SemanticRole.TECHNICAL, "WebSockets"),
        SemanticTextSpanV1(SemanticRole.TEXT, " campaign in "),
        SemanticTextSpanV1(SemanticRole.PROPER_NAME, "Region X"),
    )
    replacement_title = "".join(span.text for span in replacement_spans)
    correction_service = PublicationTextCorrectionService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    before_lead = document.lead[0].text
    old_lead_phrase = "attributed to"
    new_lead_phrase = "associated with"
    assert old_lead_phrase in before_lead
    replacement_lead = before_lead.replace(old_lead_phrase, new_lead_phrase, 1)
    first_correction = await correction_service.correct(
        PublicationTextCorrectionCommand(
            subject_id=subject.id,
            artifact_id=correction_base.id,
            artifact_version=correction_base.version,
            artifact_input_hash=correction_base.input_hash,
            artifact_sha256=hashlib.sha256(correction_base_bytes).hexdigest(),
            field_path="lead[0].text",
            expected_text=before_lead,
            replacement_text=replacement_lead,
            actor_id="human:integration-editor",
            reason="Correction de formulation avant le titre.",
            idempotency_key=f"publication-correction-lead-{uuid4().hex}",
        )
    )
    assert first_correction.artifact.canonical_blob_id is not None
    first_correction_bytes = await artifact_store.read_bytes(
        first_correction.artifact.canonical_blob_id
    )
    first_correction_payload = await artifact_store.read_json(
        first_correction.artifact.canonical_blob_id
    )
    result = await correction_service.correct(
        PublicationTextCorrectionCommand(
            subject_id=subject.id,
            artifact_id=first_correction.artifact.id,
            artifact_version=first_correction.artifact.version,
            artifact_input_hash=first_correction.artifact.input_hash,
            artifact_sha256=first_correction.canonical_sha256,
            field_path="title",
            expected_text=before_title,
            replacement_text=replacement_title,
            actor_id="human:integration-editor",
            reason="Correction exacte du titre après la phrase.",
            idempotency_key=f"publication-correction-title-{uuid4().hex}",
            replacement_spans=replacement_spans,
        )
    )
    opinion_service = AnalystOpinionService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    rebased = await opinion_service.rebase(
        opinion.id,
        opinion.version,
        opinion.content_sha256,
        result.artifact.id,
        result.canonical_sha256,
        "human:integration-reviewer",
    )
    approved = await opinion_service.approve(
        rebased.opinion.id,
        rebased.opinion.version,
        rebased.opinion.content_sha256,
        "human:integration-approver",
        "Review completed after the publication correction.",
        correlation_id=f"publication-correction-approval-{suffix}",
        bridge_drafting_gap_acknowledged=True,
    )
    published = await AnalystPublicationService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    ).publish_augmented_article(
        approved.id,
        approved.version,
        "human:integration-publisher",
        result.canonical_sha256,
    )

    assert first_correction.artifact.version == 10
    first_provenance = first_correction.artifact.metadata["manual_publication_text_correction"]
    assert first_provenance["parent_artifact_id"] == str(correction_base.id)
    assert first_provenance["validation"]["method"] == (
        "canonical_publication_qa_v5_with_declared_text_edit"
    )
    assert result.artifact.version == 11
    assert result.artifact.metadata["previous_publication_artifact_id"] == str(
        first_correction.artifact.id
    )
    assert result.artifact.metadata["manual_publication_text_correction"][
        "parent_artifact_id"
    ] == str(first_correction.artifact.id)
    assert result.artifact.metadata["manual_publication_text_correction"][
        "parent_correction_artifact_id"
    ] == str(first_correction.artifact.id)
    assert (
        result.artifact.metadata["manual_publication_text_correction"]["origin_v5_sha256"]
        == hashlib.sha256(canonical_bytes).hexdigest()
    )
    assert (
        result.artifact.metadata["manual_publication_text_correction"]["before_sha256"]
        == hashlib.sha256(first_correction_bytes).hexdigest()
    )
    assert (
        result.artifact.metadata["manual_publication_text_correction"]["before_v5_sha256"]
        == hashlib.sha256(first_correction_bytes).hexdigest()
    )
    assert (
        result.artifact.metadata["manual_publication_text_correction"]["after_sha256"]
        == result.canonical_sha256
    )
    assert result.artifact.metadata["manual_publication_text_correction"][
        "superseded_head_artifact_id"
    ] == str(first_correction.artifact.id)
    chained_validation = result.artifact.metadata["manual_publication_text_correction"][
        "validation"
    ]
    assert chained_validation["method"] == (
        "canonical_publication_qa_v5_with_declared_text_edit_chained"
    )
    assert chained_validation["checks_before"] == chained_validation["checks_after"]
    assert rebased.opinion.version == opinion.version + 1
    assert rebased.opinion.status is AnalystOpinionStatus.DRAFT
    assert rebased.opinion.blocks == opinion.blocks
    assert rebased.opinion.base_publication_artifact_id == result.artifact.id
    assert approved.version == rebased.opinion.version + 1
    assert approved.status is AnalystOpinionStatus.APPROVED
    assert approved.bridge_drafting_gap_acknowledged is True
    assert published.version == result.artifact.version + 1 == 12
    assert published.metadata["previous_publication_artifact_id"] == str(result.artifact.id)

    assert result.artifact.canonical_blob_id is not None
    corrected_payload = await artifact_store.read_json(result.artifact.canonical_blob_id)
    assert corrected_payload["schema_version"] == "5"
    expected_corrected_payload = {
        **first_correction_payload,
        "title": replacement_title,
    }
    expected_rich_text = dict(first_correction_payload["rich_text"])
    expected_paragraphs = [
        (
            {
                **item,
                "spans": [
                    {"role": span.role.value, "text": span.text} for span in replacement_spans
                ],
            }
            if item["anchor"] == "title"
            else item
        )
        for item in first_correction_payload["rich_text"]["paragraphs"]
    ]
    expected_rich_text["paragraphs"] = expected_paragraphs
    expected_corrected_payload["rich_text"] = expected_rich_text
    assert corrected_payload == expected_corrected_payload
    before_title_paragraph = next(
        item
        for item in first_correction_payload["rich_text"]["paragraphs"]
        if item["anchor"] == "title"
    )
    after_title_paragraph = next(
        item for item in corrected_payload["rich_text"]["paragraphs"] if item["anchor"] == "title"
    )
    assert corrected_payload["title"] == replacement_title
    assert corrected_payload["lead"] == first_correction_payload["lead"]
    assert corrected_payload["lead"][0]["text"] == replacement_lead
    assert after_title_paragraph["anchor"] == before_title_paragraph["anchor"] == "title"
    assert after_title_paragraph["spans"] == [
        {"role": span.role.value, "text": span.text} for span in replacement_spans
    ]
    assert "".join(span["text"] for span in after_title_paragraph["spans"]) == replacement_title
    provenance = result.artifact.metadata["manual_publication_text_correction"]
    assert provenance["semantic_spans_before"] == before_title_spans
    assert provenance["semantic_spans_after"] == after_title_paragraph["spans"]

    assert published.canonical_blob_id is not None
    published_payload = await artifact_store.read_json(published.canonical_blob_id)
    published_document = publication_document_from_json(published_payload)
    assert published_document.schema_version == "6"
    assert published_payload["base_publication"] == corrected_payload

    async with uow_factory() as uow:
        old_artifact = await uow.production_artifacts.get(base.id)
        old_requested = await uow.production_artifacts.get(requested.id)
        old_head = await uow.production_artifacts.get(head.id)
        new_opinion = await uow.analyst_opinions.get(opinion.id)
        original_opinion = await uow.analyst_opinions.get(opinion.id, version=opinion.version)
        assert old_artifact == base
        assert old_requested == requested
        assert old_head == head
        assert new_opinion == approved
        assert original_opinion == opinion

    assert base.canonical_blob_id is not None
    assert requested.canonical_blob_id is not None
    assert head.canonical_blob_id is not None
    assert await artifact_store.read_bytes(base.canonical_blob_id) == canonical_bytes
    assert await artifact_store.read_bytes(requested.canonical_blob_id) == requested_bytes
    assert await artifact_store.read_bytes(head.canonical_blob_id) == head_bytes

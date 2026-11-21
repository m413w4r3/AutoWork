from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.application.editorial_pdf_preview import PublicationPreviewRenderer
from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    production_reference_corpus_from_json,
    production_reference_corpus_to_json,
)
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.production_routing import (
    decide_and_adopt_production_routing,
)
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
)
from cti_app.application.publication_assembly import PublicationAssemblyService
from cti_app.application.publication_rendering import (
    PublicationRenderAttempt,
    PublicationRenderService,
)
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.config import Settings
from cti_app.domain.collection import CollectionState
from cti_app.domain.edition_composition import (
    CompositionDisposition,
    EditionComposition,
    EditionCompositionEntry,
)
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalModelRun,
    EditorialProposalOrigin,
    EditorialProposalState,
    variant_group_identity,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_extraction import production_extraction_to_json
from cti_app.domain.production_relevance import relevance_projection_to_json
from cti_app.domain.production_routing import ROUTING_POLICY_VERSION
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.publication_render import (
    PUBLICATION_RENDER_POLICY_VERSION,
    PublicationRender,
    compute_publication_render_input_hash,
)
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus
from cti_app.infrastructure.database.models.editorial_threads import EditorialThreadRow
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationRow,
    ModelConversationTurnRow,
    ModelRunRow,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _enrichment_seed_content,
    _EnrichmentSeedContent,
    _Seeded,
)
from tests.integration.editorial.test_table_insertion_postgres import (
    _ready_for_manual_table_route,
)

pytestmark = pytest.mark.integration

_FAKE_PDF = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


class _ExplicitFakePublicationRenderer:
    """Persist a typed render row and PDF blob for offline PostgreSQL integration."""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, artifact_store: ProductionArtifactStore
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def render_preview(self, publication_artifact_id: UUID) -> PublicationRenderAttempt:
        async with self._uow_factory() as uow:
            artifact = await uow.production_artifacts.get(publication_artifact_id)
            assert artifact is not None and artifact.canonical_blob_id is not None
            canonical_sha256 = hashlib.sha256(
                await self._artifact_store.read_bytes(artifact.canonical_blob_id)
            ).hexdigest()
            identity = compute_publication_render_input_hash(
                publication_artifact_id=artifact.id,
                publication_content_sha256=canonical_sha256,
                renderer="explicit-fake-test-renderer",
                renderer_version="1",
                template_version="integration-fixture-v1",
                template_sha256="a" * 64,
                compiler="fake-test-compiler",
                compiler_version="1",
                format=TypstRenderFormat.PDF,
                font_bundle_version="integration-fixture-fonts-v1",
                render_policy_version=PUBLICATION_RENDER_POLICY_VERSION,
            )
            existing = await uow.publication_renders.get_by_input_hash(identity)
            if existing is not None:
                return PublicationRenderAttempt(existing.status, existing)

        output_blob_id = await self._artifact_store.put_bytes(
            _FAKE_PDF,
            bucket="publication-render-output",
            mime_type="application/pdf",
        )
        now = datetime.now(UTC)
        render = PublicationRender(
            id=uuid4(),
            publication_artifact_id=publication_artifact_id,
            renderer="explicit-fake-test-renderer",
            renderer_version="1",
            template_version="integration-fixture-v1",
            template_sha256="a" * 64,
            compiler="fake-test-compiler",
            compiler_version="1",
            font_bundle_version="integration-fixture-fonts-v1",
            render_policy_version=PUBLICATION_RENDER_POLICY_VERSION,
            format=TypstRenderFormat.PDF,
            input_hash=identity,
            source_blob_id=None,
            render_data_blob_id=None,
            output_blob_id=output_blob_id,
            output_sha256=hashlib.sha256(_FAKE_PDF).hexdigest(),
            output_byte_size=len(_FAKE_PDF),
            status=TypstRenderStatus.SUCCEEDED,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        async with self._uow_factory() as uow:
            existing = await uow.publication_renders.get_by_input_hash(identity)
            if existing is not None:
                return PublicationRenderAttempt(existing.status, existing)
            await uow.publication_renders.add(render)
            await uow.commit()
        return PublicationRenderAttempt(TypstRenderStatus.SUCCEEDED, render)


def _publication_renderer(
    app: FastAPI, uow_factory: UnitOfWorkFactory, artifact_store: ProductionArtifactStore
) -> PublicationPreviewRenderer:
    binary = os.environ.get("TYPST_BINARY")
    media_assets = MediaAssetStore(artifact_store, uow_factory)
    app.state.media_asset_store = media_assets
    if not binary:
        return _ExplicitFakePublicationRenderer(uow_factory, artifact_store)
    chp_root, font_root, font_lock = typst_bundle_paths()
    return PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=media_assets,
        renderer=TypstRenderer(),
        compiler=TypstSubprocessCompiler(binary=binary),
        chp_typst_root=chp_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=font_lock,
    )


async def _seed_current_inputs(
    seeded: _Seeded,
    app: FastAPI,
    uow_factory: UnitOfWorkFactory,
) -> _EnrichmentSeedContent:
    artifact_store = cast(ProductionArtifactStore, app.state.production_artifact_store)
    source = seeded.extraction.sources[0]
    references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=seeded.subject.id,
        research_date=seeded.production_input.research_date,
        production_input_hash=seeded.production_input.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url=source.canonical_url,
                tier=source.tier,
                kind=source.kind,
                role=source.role,
                title="Synthetic adoption source",
                publisher="Synthetic publisher",
                published_at=None,
                source_collection_id=None,
                source_document_id=source.source_document_id,
                discovery_candidate_ids=(),
                collection_state=CollectionState.EXTRACTED,
                content_sha256=source.content_sha256,
                relevance_reason=None,
                proposed_by_model=False,
                eligible_for_extraction=True,
                editorial_role=source.editorial_role,
            ),
        ),
        warnings=(),
    )
    references_payload = production_reference_corpus_to_json(references)
    references = production_reference_corpus_from_json(references_payload)
    extraction = replace(
        seeded.extraction,
        references_corpus_hash=references_corpus_hash(references),
    )
    projection = build_relevance_projection(seeded.production_input, extraction)
    synthesis = replace(
        seeded.synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
    )
    enrichment = _enrichment_seed_content(seeded.production_input, extraction, synthesis)
    synthesis_payload = production_synthesis_to_json(synthesis)
    synthesis_artifact_id = uuid4()

    references_blob_id = await artifact_store.put_json(
        references_payload, bucket="production-artifacts-canonical"
    )
    extraction_blob_id = await artifact_store.put_json(
        production_extraction_to_json(extraction), bucket="production-artifacts-canonical"
    )
    projection_blob_id = await artifact_store.put_json(
        relevance_projection_to_json(projection), bucket="production-artifacts-canonical"
    )
    synthesis_blob_id = await artifact_store.put_json(
        synthesis_payload, bucket="production-artifacts-canonical"
    )

    references_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash=seeded.production_input.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=references_blob_id,
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=2,
        input_hash=seeded.production_input.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob_id,
    )
    projection_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash=projection.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=projection_blob_id,
    )
    synthesis_artifact = ProductionArtifact(
        id=synthesis_artifact_id,
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=2,
        input_hash=seeded.production_input.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=synthesis_blob_id,
    )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(references_artifact)
        await uow.production_artifacts.append(extraction_artifact)
        await uow.production_artifacts.append(projection_artifact)
        await uow.production_artifacts.append(synthesis_artifact)
        await uow.commit()

    workspace_service = EditorialWorkspaceService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    async with uow_factory() as uow:
        workspace_record = await workspace_service.snapshot_record_in_uow(uow, seeded.subject.id)
        await uow.commit()
    assert workspace_record.synthesis_artifact_id == synthesis_artifact.id
    assert workspace_record.synthesis_version == synthesis_artifact.version
    assert workspace_record.enrichment_artifact_id is not None

    collection_service = editorial_workspace_api._collection_service_for_request(
        SimpleNamespace(app=app)
    )
    media_archiver = getattr(collection_service, "source_media_archiver", None)
    media_policy_sha256 = getattr(media_archiver, "policy_sha256", None)
    if not isinstance(media_policy_sha256, str):
        media_policy_sha256 = None
    async with uow_factory() as uow:
        access_policy = await build_synthesis_access_policy(
            seeded.production_input,
            extraction,
            uow.source_documents,
            uow.source_collections,
        )
        inventory = await load_archived_source_figure_inventory(
            subject_id=seeded.subject.id,
            extraction_sources=extraction.sources,
            source_document_repository=uow.source_documents,
            blob_repository=uow.blobs,
            artifact_store=artifact_store,
            source_media_repository=uow.source_media_candidates,
            media_policy_sha256=media_policy_sha256,
        )
    access_policy_hash = synthesis_access_policy_hash(access_policy)
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input,
        extraction,
        synthesis,
        projection,
        workspace_record,
    )
    evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    inventory_hash = inventory.functional_hash()
    enrichment_input_hash = compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
        projection_hash=projection.projection_hash,
        source_figure_inventory_hash=inventory_hash,
        resource_search_enabled=False,
    )

    enrichment_blob_id = await artifact_store.put_json(
        enrichment.base_payload, bucket="production-artifacts-canonical"
    )
    enrichment_artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=2,
        input_hash=enrichment_input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "access_policy_hash": access_policy_hash,
            "synthesis_access_policy_hash": access_policy_hash,
            "evidence_pack_hash": evidence_pack_hash,
            "source_figure_inventory_hash": inventory_hash,
            "routing_policy_version": ROUTING_POLICY_VERSION,
        },
        canonical_blob_id=enrichment_blob_id,
    )

    async with uow_factory() as uow:
        await uow.production_artifacts.append(enrichment_artifact)
        run = await uow.production_runs.get(seeded.run.id)
        assert run is not None
        expected_version = run.version
        await uow.commit()

    routing_decision = await decide_and_adopt_production_routing(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        production_run_id=seeded.run.id,
        expected_version=expected_version,
    )

    assert routing_decision.publication_kind is PublicationKind.BRIEF
    async with uow_factory() as uow:
        run = await uow.production_runs.get(seeded.run.id)
        enrichment_artifact = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
        assert run is not None and enrichment_artifact is not None
        assert run.routing_decision_id == routing_decision.decision_id
        assembly = PublicationAssemblyService(artifact_store, uow.production_artifacts)
        await assembly.assemble_publication(
            run=run,
            snapshot=seeded.production_input,
            references=references,
            extraction=extraction,
            relevance_projection=projection,
            synthesis=synthesis,
            editorial_enrichment=enrichment.base_enrichment,
            publication_kind=routing_decision.publication_kind,
            editorial_enrichment_artifact_id=enrichment_artifact.id,
            editorial_enrichment_artifact_input_hash=enrichment_artifact.input_hash,
            routing_decision_hash=routing_decision.decision_hash,
            routing_policy_version=routing_decision.policy_version,
            metadata_extra={
                "input_artifacts": {
                    "references_artifact_id": str(references_artifact.id),
                    "extraction_artifact_id": str(extraction_artifact.id),
                    "relevance_projection_artifact_id": str(projection_artifact.id),
                    "synthesis_artifact_id": str(synthesis_artifact.id),
                    "editorial_enrichment_artifact_id": str(enrichment_artifact.id),
                }
            },
        )
        await uow.commit()
    return enrichment


async def _finish_seeded_production_run(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
) -> _Seeded:
    async with uow_factory() as uow:
        run = await uow.production_runs.get(seeded.run.id)
        assert run is not None
        assert run.status is ProductionRunStatus.QUEUED
        assert run.current_stage is ProductionStage.SOURCES
        assert run.pipeline_generation == 0
        now = datetime.now(UTC)
        run.start_running(now=now)
        while run.current_stage is not ProductionStage.ASSEMBLY:
            run.advance_stage(publication_kind=PublicationKind.BRIEF, now=now)
        run.mark_ready(now=now)
        await uow.production_runs.save(run)
        await uow.commit()

    async with uow_factory() as uow:
        persisted_run = await uow.production_runs.get(seeded.run.id)
    assert persisted_run is not None
    assert persisted_run.status is ProductionRunStatus.READY
    assert persisted_run.current_stage is ProductionStage.ASSEMBLY
    assert persisted_run.pipeline_generation == 0
    assert persisted_run.finished_at is not None
    return replace(seeded, run=persisted_run)


async def _seed_bridge_history_proposal(
    *,
    migrated_postgres_url: str,
    uow_factory: UnitOfWorkFactory,
    base_proposal: EditorialProposal,
) -> EditorialProposal:
    now = datetime.now(UTC)
    thread_id = uuid4()
    conversation_id = uuid4()
    model_run_id = uuid4()
    model_turn_id = uuid4()
    request_sha256 = hashlib.sha256(uuid4().bytes).hexdigest()
    idempotency_key = f"bridge-history-{uuid4().hex}"
    bridge_proposal = replace(
        base_proposal,
        id=uuid4(),
        origin=EditorialProposalOrigin.BRIDGE_DRAFT,
        state=EditorialProposalState.PROPOSED,
        state_version=1,
        thread_id=thread_id,
        variant_group_id=variant_group_identity(
            base_pin_hash=base_proposal.base_pin_hash,
            occurrence_id=base_proposal.occurrence_id,
            operation=base_proposal.operation,
            thread_id=thread_id,
        ),
        model_run_id=model_run_id,
        model_turn_id=model_turn_id,
        decision_id=None,
        accepted_artifact_id=None,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        created_at=now,
        updated_at=now,
    )
    event = EditorialProposalEvent(
        id=uuid4(),
        proposal_id=bridge_proposal.id,
        sequence=1,
        kind=EditorialProposalEventKind.PREPARED,
        state=EditorialProposalState.PROPOSED,
        state_version=1,
        actor_id=bridge_proposal.actor_id,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        patch_sha256=bridge_proposal.patch_sha256,
    )
    model_link = EditorialProposalModelRun(
        proposal_id=bridge_proposal.id,
        thread_id=thread_id,
        model_run_id=model_run_id,
        model_turn_id=model_turn_id,
    )
    async with uow_factory() as uow:
        subject = await uow.subjects.get(base_proposal.subject_id)
    assert subject is not None

    engine = create_postgres_engine(migrated_postgres_url)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session, session.begin():
            session.add(
                ModelRunRow(
                    id=model_run_id,
                    provider="fake",
                    backend="fake",
                    transport="fake",
                    model_role="drafting",
                    requested_model="synthetic",
                    prompt_template_id="synthetic-editorial-proposal",
                    prompt_template_version="1",
                    authorized_input_hash="1" * 64,
                    evidence_pack_hash="2" * 64,
                    parameters={},
                    status="succeeded",
                    submission_state="result_obtained",
                    submission_attempt=1,
                    output_references=["synthetic-model-output"],
                    validation_errors=[],
                    transformations=[],
                    citation_count=0,
                    extracted_url_count=0,
                    visible_citations=[],
                    started_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            conversation = ModelConversationRow(
                id=conversation_id,
                provider="fake",
                transport="application_managed",
                purpose="editorial_discussion",
                edition_id=subject.edition_id,
                subject_id=base_proposal.subject_id,
                title="Synthetic editorial conversation",
                status="ready",
                head_turn_id=None,
                turn_count=0,
                created_at=now,
                updated_at=now,
                last_used_at=None,
                version=1,
            )
            session.add(conversation)
            await session.flush()
            session.add(
                ModelConversationTurnRow(
                    id=model_turn_id,
                    conversation_id=conversation_id,
                    sequence=1,
                    parent_turn_id=None,
                    model_run_id=model_run_id,
                    input_blob_reference="synthetic-input",
                    input_sha256="3" * 64,
                    output_blob_reference="synthetic-output",
                    output_sha256="4" * 64,
                    status="succeeded",
                    external_turn_id=None,
                    idempotency_key=f"synthetic-turn-{uuid4().hex}",
                    correlation_id="synthetic-correlation",
                    error_code=None,
                    error_message=None,
                    error_details=None,
                    objective="editorial_proposal",
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                )
            )
            await session.flush()
            conversation.head_turn_id = model_turn_id
            conversation.turn_count = 1
            session.add(
                EditorialThreadRow(
                    id=thread_id,
                    subject_id=bridge_proposal.subject_id,
                    production_run_id=bridge_proposal.production_run_id,
                    occurrence_id=bridge_proposal.occurrence_id,
                    session_number=1,
                    origin_artifact_id=bridge_proposal.base_artifact_id,
                    origin_artifact_version=bridge_proposal.base_artifact_version,
                    origin_canonical_sha256=bridge_proposal.base_canonical_sha256,
                    model_conversation_id=conversation_id,
                    generation_state="reply_available",
                    next_sequence=2,
                    version=2,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

        async with uow_factory() as uow:
            stored, inserted = await uow.editorial_proposals.add_if_absent(
                bridge_proposal,
                event=event,
                model_run=model_link,
            )
            assert inserted
            await uow.commit()
        return stored
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_editorial_pdf_postgres_adoption_regeneration_history_and_composition_pin(
    uow_factory: UnitOfWorkFactory,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(_env_file=None, app_env="development", api_bind_host="127.0.0.1"),
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            editorial_proposal_accept_enabled=True,
            production_editorial_resource_search_enabled=False,
        ),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    seeded, app, _model_gateway, _old_enrichment = await _ready_for_manual_table_route(uow_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        initial_snapshot_response = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/snapshot"
        )
    assert initial_snapshot_response.status_code == 200, initial_snapshot_response.text
    initial_snapshot = initial_snapshot_response.json()
    assert initial_snapshot["publication_kind"] == PublicationKind.BRIEF.value
    initial_anchor = next(
        block
        for block in initial_snapshot["blocks"]
        if block["kind"] in {"paragraph", "lead_paragraph"}
    )
    assert initial_anchor["revision_status"] == "ready"

    enrichment = await _seed_current_inputs(seeded, app, uow_factory)
    seeded = await _finish_seeded_production_run(seeded, uow_factory)
    artifact_store = cast(ProductionArtifactStore, app.state.production_artifact_store)
    app.state.publication_render_service = _publication_renderer(app, uow_factory, artifact_store)

    async with uow_factory() as uow:
        old_publication = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.PUBLICATION.value
        )
        assert old_publication is not None
        composition = EditionComposition(
            edition_id=seeded.run.edition_id,
            version=1,
            actor_id="dev-analyst",
            entries=(
                EditionCompositionEntry(
                    subject_id=seeded.subject.id,
                    disposition=CompositionDisposition.INCLUDED,
                    position=1,
                    production_run_id=seeded.run.id,
                    pipeline_generation=seeded.run.pipeline_generation,
                    artifact_id=old_publication.id,
                    artifact_version=old_publication.version,
                    artifact_input_hash=old_publication.input_hash,
                ),
            ),
        )
        await uow.edition_compositions.save(composition)
        await uow.edition_compositions.replace_entries(composition.id, composition.entries)
        await uow.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot_response = await client.get(f"/api/subjects/{seeded.subject.id}/editor/snapshot")
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        assert snapshot["publication_kind"] == PublicationKind.BRIEF.value
        anchor = next(
            block
            for block in snapshot["blocks"]
            if block["kind"] in {"paragraph", "lead_paragraph"}
        )
        assert anchor["revision_status"] == "ready"
        anchor_id = UUID(anchor["block_id"])
        assert anchor_id == UUID(initial_anchor["block_id"])
        input_artifacts = old_publication.metadata.get("input_artifacts")
        assert isinstance(input_artifacts, dict)
        assert input_artifacts["synthesis_artifact_id"] == snapshot["synthesis_artifact_id"]
        assert (
            input_artifacts["editorial_enrichment_artifact_id"]
            == snapshot["enrichment_artifact_id"]
        )
        evidence = enrichment.base_enrichment.tables[0].rows[0].evidence_refs[0]
        evidence_ref = {
            "source_document_id": str(evidence.source_document_id),
            "kind": evidence.kind.value,
            "evidence_key": evidence.evidence_key,
        }
        proposal_response = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/blocks/{anchor_id}/table-proposals",
            headers={"Idempotency-Key": f"pdf-history-table-{uuid4().hex}"},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "anchor_block_id": str(anchor_id),
                "table": {
                    "key": "pdf_history_inserted_table",
                    "kind": "custom",
                    "title": "Synthetic findings",
                    "caption": "Synthetic findings at the selected paragraph.",
                    "columns": [
                        {"key": "finding", "label": "Finding"},
                        {"key": "source", "label": "Source detail"},
                    ],
                    "rows": [
                        {
                            "cells": ["Synthetic finding", "Synthetic test context."],
                            "evidence_refs": [evidence_ref],
                        },
                        {
                            "cells": [
                                "Synthetic finding",
                                "The synthetic report records this finding.",
                            ],
                            "evidence_refs": [evidence_ref],
                        },
                    ],
                    "purpose": {
                        "question": "Which source passage supports this finding?",
                        "available_data": "The cited synthetic finding and source context.",
                        "comprehension_gain": "Compare the finding with its source context.",
                        "scope": "Only the cited synthetic finding.",
                        "evidence_refs": [evidence_ref],
                        "knowledge_limits": "No additional source detail is represented.",
                        "placement_reason": "Keep the table after its anchor paragraph.",
                    },
                },
            },
        )
        assert proposal_response.status_code == 200, proposal_response.text
        proposal_id = UUID(proposal_response.json()["id"])
        async with uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence(
                seeded.subject.id, anchor_id, proposal_id
            )
        assert proposal is not None
        accepted = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/threads/{anchor_id}/"
            f"proposals/{proposal_id}/accept",
            headers={"Idempotency-Key": f"pdf-history-accept-{uuid4().hex}"},
            json={
                "expected_state_version": proposal.state_version,
                "patch_sha256": proposal.patch_sha256,
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["state"] == EditorialProposalState.ACCEPTED.value
        assert accepted.json()["impact_state"] == "pending"
        assert accepted.json()["impact_id"]

        stale = await client.get(f"/api/subjects/{seeded.subject.id}/editor/pdf")
        assert stale.status_code == 200, stale.text
        assert stale.json()["status"] == "STALE"
        assert stale.json()["artifact_id"] == str(old_publication.id)
        assert stale.json()["artifact_version"] == old_publication.version
        assert stale.json()["artifact_input_hash"] == old_publication.input_hash
        assert stale.json()["pdf_url"] is None

        regenerated = await client.post(f"/api/subjects/{seeded.subject.id}/editor/pdf/regenerate")
        assert regenerated.status_code == 200, regenerated.text
        ready = regenerated.json()
        assert ready["status"] == "READY"
        assert ready["artifact_id"] != str(old_publication.id)
        assert ready["render_id"]
        assert ready["render_identity"]
        assert ready["pdf_url"].endswith(f"render_identity={ready['render_identity']}")
        pdf_response = await client.get(ready["pdf_url"])
        assert pdf_response.status_code == 200
        assert pdf_response.content.startswith(b"%PDF-")

        async with uow_factory() as uow:
            ready_artifact = await uow.production_artifacts.get(UUID(ready["artifact_id"]))
            ready_render = await uow.publication_renders.get(UUID(ready["render_id"]))
        assert ready_artifact is not None and ready_artifact.canonical_blob_id is not None
        assert ready_render is not None
        assert ready_render.status is TypstRenderStatus.SUCCEEDED
        assert ready_render.publication_artifact_id == ready_artifact.id
        assert ready_render.input_hash == ready["render_identity"]
        assert ready["artifact_version"] == ready_artifact.version
        assert ready["artifact_input_hash"] == ready_artifact.input_hash
        assert (
            ready["artifact_canonical_sha256"]
            == hashlib.sha256(
                await artifact_store.read_bytes(ready_artifact.canonical_blob_id)
            ).hexdigest()
        )
        assert hashlib.sha256(pdf_response.content).hexdigest() == ready_render.output_sha256

        composition_status = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/composition"
        )
        assert composition_status.status_code == 200, composition_status.text
        assert composition_status.json()["status"] == "stale"
        assert composition_status.json()["message"] == "Composition à mettre à jour"
        assert composition_status.json()["pinned_artifact_id"] == str(old_publication.id)
        assert composition_status.json()["pinned_artifact_version"] == old_publication.version
        assert composition_status.json()["current_artifact_id"] == ready["artifact_id"]

        first_history = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/history?limit=20"
        )
        assert first_history.status_code == 200, first_history.text
        assert any(
            item["proposal_id"] == str(proposal_id)
            and item["origin"] == EditorialProposalOrigin.MANUAL_PATCH.value
            and item["state"] == EditorialProposalState.ACCEPTED.value
            for item in first_history.json()["items"]
        )

    async with uow_factory() as uow:
        stored_composition = await uow.edition_compositions.get_for_edition(seeded.run.edition_id)
    assert stored_composition is not None
    assert stored_composition.version == composition.version
    assert stored_composition.entries == composition.entries

    async with uow_factory() as uow:
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, anchor_id, proposal_id
        )
    assert stored is not None
    await _seed_bridge_history_proposal(
        migrated_postgres_url=migrated_postgres_url,
        uow_factory=uow_factory,
        base_proposal=stored,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        first_bridge_page = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/history",
            params={"limit": 1},
        )
        assert first_bridge_page.status_code == 200, first_bridge_page.text
        first_page = first_bridge_page.json()
        assert len(first_page["items"]) == 1
        assert first_page["items"][0]["origin"] == EditorialProposalOrigin.BRIDGE_DRAFT.value
        assert first_page["items"][0]["state"] == EditorialProposalState.PROPOSED.value
        assert first_page["next_cursor"]

        second_history_page = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/history",
            params={"limit": 1, "cursor": first_page["next_cursor"]},
        )
    assert second_history_page.status_code == 200, second_history_page.text
    second_page = second_history_page.json()
    assert len(second_page["items"]) == 1
    assert second_page["items"][0]["proposal_id"] == str(proposal_id)
    assert second_page["items"][0]["origin"] == EditorialProposalOrigin.MANUAL_PATCH.value
    assert second_page["items"][0]["state"] == EditorialProposalState.ACCEPTED.value
    assert second_page["next_cursor"] is None

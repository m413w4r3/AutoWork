from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
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
    ROUTING_POLICY_VERSION,
    decide_and_adopt_production_routing,
)
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_rendering import (
    PublicationRenderAttempt,
    PublicationRenderService,
)
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
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalOrigin,
    EditorialProposalState,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_extraction import production_extraction_to_json
from cti_app.domain.production_relevance import relevance_projection_to_json
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.publication_render import (
    PUBLICATION_RENDER_POLICY_VERSION,
    PublicationRender,
    compute_publication_render_input_hash,
)
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus
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

    def __init__(self, uow_factory: Any, artifact_store: ProductionArtifactStore) -> None:
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
    app: FastAPI, uow_factory: Any, artifact_store: ProductionArtifactStore
) -> Any:
    binary = os.environ.get("TYPST_BINARY")
    if not binary:
        return _ExplicitFakePublicationRenderer(uow_factory, artifact_store)
    chp_root, font_root, font_lock = typst_bundle_paths()
    media_assets = MediaAssetStore(artifact_store, uow_factory)
    app.state.media_asset_store = media_assets
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
    uow_factory: Any,
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
        production_synthesis_to_json(synthesis), bucket="production-artifacts-canonical"
    )
    enrichment_blob_id = await artifact_store.put_json(
        enrichment.base_payload, bucket="production-artifacts-canonical"
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
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=2,
        input_hash=seeded.production_input.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=synthesis_blob_id,
    )

    async with uow_factory() as uow:
        old_enrichment = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
        metadata = dict(old_enrichment.metadata) if old_enrichment is not None else {}
        metadata["routing_policy_version"] = ROUTING_POLICY_VERSION
        await uow.production_artifacts.append(references_artifact)
        await uow.production_artifacts.append(extraction_artifact)
        await uow.production_artifacts.append(projection_artifact)
        await uow.production_artifacts.append(synthesis_artifact)
        await uow.production_artifacts.append(
            ProductionArtifact(
                production_run_id=seeded.run.id,
                subject_id=seeded.subject.id,
                stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                version=2,
                input_hash="e" * 64,
                status=ProductionArtifactStatus.VERIFIED,
                metadata=metadata,
                canonical_blob_id=enrichment_blob_id,
            )
        )
        run = await uow.production_runs.get(seeded.run.id)
        assert run is not None
        expected_version = run.version
        await uow.commit()

    await decide_and_adopt_production_routing(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        production_run_id=seeded.run.id,
        expected_version=expected_version,
    )
    return enrichment


@pytest.mark.asyncio
async def test_editorial_pdf_postgres_adoption_regeneration_history_and_composition_pin(
    uow_factory: Any,
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
    enrichment = await _seed_current_inputs(seeded, app, uow_factory)
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
        anchor_id = UUID(anchor["block_id"])
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

        stale = await client.get(f"/api/subjects/{seeded.subject.id}/editor/pdf")
        assert stale.status_code == 200, stale.text
        assert stale.json()["status"] == "STALE"
        assert stale.json()["artifact_id"] == str(old_publication.id)
        assert stale.json()["pdf_url"] is None

        regenerated = await client.post(f"/api/subjects/{seeded.subject.id}/editor/pdf/regenerate")
        assert regenerated.status_code == 200, regenerated.text
        ready = regenerated.json()
        assert ready["status"] == "READY"
        assert ready["artifact_id"] != str(old_publication.id)
        assert ready["render_identity"]
        assert ready["pdf_url"].endswith(f"render_identity={ready['render_identity']}")
        pdf_response = await client.get(ready["pdf_url"])
        assert pdf_response.status_code == 200
        assert pdf_response.content.startswith(b"%PDF-")

        composition_status = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/composition"
        )
        assert composition_status.status_code == 200, composition_status.text
        assert composition_status.json()["status"] == "stale"
        assert composition_status.json()["message"] == "Composition à mettre à jour"
        assert composition_status.json()["pinned_artifact_id"] == str(old_publication.id)
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
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, anchor_id, proposal_id
        )
        assert stored is not None
        bridge_proposal = replace(
            stored,
            id=uuid4(),
            origin=EditorialProposalOrigin.BRIDGE_DRAFT,
            state=EditorialProposalState.PROPOSED,
            state_version=1,
            thread_id=uuid4(),
            model_run_id=None,
            model_turn_id=None,
            decision_id=None,
            accepted_artifact_id=None,
            idempotency_key=f"bridge-history-{uuid4().hex}",
            request_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        event = EditorialProposalEvent(
            id=uuid4(),
            proposal_id=bridge_proposal.id,
            sequence=1,
            kind=EditorialProposalEventKind.PREPARED,
            state=EditorialProposalState.PROPOSED,
            state_version=1,
            actor_id=bridge_proposal.actor_id,
            idempotency_key=bridge_proposal.idempotency_key,
            request_sha256=bridge_proposal.request_sha256,
            patch_sha256=bridge_proposal.patch_sha256,
        )
        await uow.editorial_proposals.add_if_absent(bridge_proposal, event=event, model_run=None)
        await uow.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        after_reload = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/history?limit=20"
        )
    assert after_reload.status_code == 200, after_reload.text
    origins = {item["origin"] for item in after_reload.json()["items"]}
    assert EditorialProposalOrigin.MANUAL_PATCH.value in origins
    assert EditorialProposalOrigin.BRIDGE_DRAFT.value in origins

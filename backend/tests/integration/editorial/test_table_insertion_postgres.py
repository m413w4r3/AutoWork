from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.api.main import create_app
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
    editorial_enrichment_from_json,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
)
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    synthesis_access_policy_hash,
)
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_proposal import EditorialProposalOrigin, EditorialProposalState
from cti_app.domain.editorial_workspace import EditorialSourceStage, bootstrap_block_registry
from cti_app.domain.entities import SourceDocument
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_extraction import production_extraction_to_json
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_document import PublicationDocumentV9, parse_publication_document
from cti_app.domain.publication_kind import PublicationKind
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _enrichment_seed_content,
    _seed,
    _Seeded,
)
from tests.test_publication_assembly import _assemble
from tests.test_publication_assembly import _service as _publication_service
from tests.test_publication_kind_resolution import _brief_v8_payload

pytestmark = pytest.mark.integration


_CANONICAL_BUCKET = "production-artifacts-canonical"


class _NoModelCalls:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def unexpected_call(*_args: Any, **_kwargs: Any) -> None:
            self.calls.append(name)
            raise AssertionError("Manual table insertion must not call the model gateway")

        return unexpected_call


async def _ready_for_manual_table_route(
    uow_factory: Any,
) -> tuple[_Seeded, Any, _NoModelCalls, Any]:
    # Use the real discovery lineage and persist the rows required by the
    # production discussion/adoption adapters. Blob IDs come from add_if_absent
    # because the PostgreSQL integration database is shared between tests.
    seeded = await _seed(
        uow_factory,
        proposal_count=0,
        seed_discovery_lineage=True,
    )
    enrichment_content = _enrichment_seed_content(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
    )
    enrichment_bytes = ProductionArtifactStore.canonical_json_bytes(enrichment_content.base_payload)
    extraction_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_extraction_to_json(seeded.extraction)
    )
    source_bytes = b"synthetic manual-table integration source"
    publication_bytes = ProductionArtifactStore.canonical_json_bytes(_brief_v8_payload())

    def blob_record(content: bytes, mime_type: str, bucket: str) -> BlobRecord:
        return BlobRecord(
            descriptor=BlobDescriptor(
                hashlib.sha256(content).hexdigest(), len(content), mime_type, bucket
            )
        )

    enrichment_blob = blob_record(enrichment_bytes, "application/json", _CANONICAL_BUCKET)
    extraction_blob = blob_record(extraction_bytes, "application/json", _CANONICAL_BUCKET)
    source_blob = blob_record(source_bytes, "text/plain", "source-documents")
    publication_blob = blob_record(publication_bytes, "application/json", _CANONICAL_BUCKET)
    for blob, content in (
        (enrichment_blob, enrichment_bytes),
        (extraction_blob, extraction_bytes),
        (source_blob, source_bytes),
        (publication_blob, publication_bytes),
    ):
        seeded.blob_store.seed(blob.descriptor, content)

    async with uow_factory() as uow:
        stored_enrichment_blob = await uow.blobs.add_if_absent(enrichment_blob)
        stored_extraction_blob = await uow.blobs.add_if_absent(extraction_blob)
        stored_source_blob = await uow.blobs.add_if_absent(source_blob)
        stored_publication_blob = await uow.blobs.add_if_absent(publication_blob)
        await uow.production_input_snapshots.add(seeded.production_input)
        await uow.source_documents.add(
            SourceDocument(
                id=seeded.extraction.sources[0].source_document_id,
                subject_id=seeded.subject.id,
                blob_id=stored_source_blob.id,
                original_name="synthetic-report.txt",
                origin="synthetic integration fixture",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=TLP.AMBER,
                do_not_submit=False,
                external_llm_allowed=True,
            )
        )
        await uow.production_artifacts.append(
            ProductionArtifact(
                production_run_id=seeded.run.id,
                subject_id=seeded.subject.id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=1,
                input_hash=seeded.production_input.input_hash,
                canonical_blob_id=stored_extraction_blob.id,
            )
        )
        await uow.commit()

    blob_store = cast(BlobStore, seeded.blob_store)
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    synthesis_payload = production_synthesis_to_json(seeded.synthesis)
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(synthesis_payload)
    ).hexdigest()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=seeded.base_artifact.id,
        canonical_sha256=synthesis_sha256,
        source_stage=EditorialSourceStage.SYNTHESIS,
        canonical_content=synthesis_payload,
    )
    enrichment_workspace_view = SimpleNamespace(
        subject_id=seeded.subject.id,
        production_run_id=seeded.run.id,
        synthesis_artifact_id=seeded.base_artifact.id,
        synthesis_version=seeded.base_artifact.version,
        synthesis_canonical_sha256=synthesis_sha256,
        block_registry=synthesis_entries,
    )
    async with uow_factory() as uow:
        access_policy = await build_synthesis_access_policy(
            seeded.production_input,
            seeded.extraction,
            uow.source_documents,
            uow.source_collections,
        )
        inventory = await load_archived_source_figure_inventory(
            subject_id=seeded.subject.id,
            extraction_sources=seeded.extraction.sources,
            source_document_repository=uow.source_documents,
            blob_repository=uow.blobs,
            artifact_store=artifact_store,
        )
    access_policy_hash = synthesis_access_policy_hash(access_policy)
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
        workspace_record=enrichment_workspace_view,
    )
    evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    inventory_hash = inventory.functional_hash()
    enrichment_input_hash = compute_editorial_enrichment_input_hash(
        extraction=seeded.extraction,
        synthesis=seeded.synthesis,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
        source_figure_inventory_hash=inventory_hash,
        resource_search_enabled=False,
    )
    enrichment_artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash=enrichment_input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "access_policy_hash": access_policy_hash,
            "synthesis_access_policy_hash": access_policy_hash,
            "evidence_pack_hash": evidence_pack_hash,
            "source_figure_inventory_hash": inventory_hash,
            "routing_policy_version": "synthetic-routing-policy",
        },
        canonical_blob_id=stored_enrichment_blob.id,
    )
    publication_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        input_hash=hashlib.sha256(
            f"{seeded.base_artifact.id}:{enrichment_artifact.id}".encode()
        ).hexdigest(),
        metadata={
            "input_artifacts": {
                "synthesis_artifact_id": str(seeded.base_artifact.id),
                "editorial_enrichment_artifact_id": str(enrichment_artifact.id),
            }
        },
        canonical_blob_id=stored_publication_blob.id,
    )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(enrichment_artifact)
        await uow.production_artifacts.append(publication_artifact)
        await uow.commit()

    app = create_app()
    app.state.uow_factory = uow_factory
    app.state.blob_store = blob_store
    app.state.production_artifact_store = artifact_store
    model_gateway = _NoModelCalls()
    app.state.model_gateway = model_gateway
    app.state.publication_render_service = object()
    from cti_app.application.identity import LocalIdentityProvider

    app.state.identity_provider = LocalIdentityProvider("dev-analyst")
    return seeded, app, model_gateway, enrichment_content


@pytest.mark.asyncio
async def test_manual_table_patch_accept_publishes_table_at_registered_anchor(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            app_env="development",
            api_bind_host="127.0.0.1",
        ),
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
    seeded, app, model_gateway, enrichment_content = await _ready_for_manual_table_route(
        uow_factory
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        snapshot_response = await client.get(f"/api/subjects/{seeded.subject.id}/editor/snapshot")
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        anchor_block = next(
            block
            for block in snapshot["blocks"]
            if block["kind"] in {"paragraph", "lead_paragraph"}
        )
        anchor_id = UUID(anchor_block["block_id"])
        evidence = enrichment_content.base_enrichment.tables[0].rows[0].evidence_refs[0]
        evidence_ref = {
            "source_document_id": str(evidence.source_document_id),
            "kind": evidence.kind.value,
            "evidence_key": evidence.evidence_key,
        }
        table_key = "table_inserted_after_registered_paragraph"
        manual_table = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/blocks/{anchor_id}/table-proposals",
            headers={"Idempotency-Key": f"manual-table-{uuid4().hex}"},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "anchor_block_id": str(anchor_id),
                "table": {
                    "key": table_key,
                    "kind": "custom",
                    "title": "Inserted synthetic findings",
                    "caption": "Synthetic findings placed after the selected paragraph.",
                    "columns": [
                        {"key": "finding", "label": "Finding"},
                        {"key": "source", "label": "Source detail"},
                    ],
                    "rows": [
                        {
                            "cells": ["Synthetic finding", "Synthetic test context."],
                            "evidence_refs": [evidence_ref],
                        }
                    ],
                    "purpose": {
                        "question": "Which source passage supports this finding?",
                        "available_data": "The cited synthetic source passage.",
                        "comprehension_gain": "Inspect the cited passage beside the finding.",
                        "scope": "Only the synthetic finding and its cited passage.",
                        "evidence_refs": [evidence_ref],
                        "knowledge_limits": "No other source detail is represented.",
                        "placement_reason": "Keep the table after its anchor paragraph.",
                    },
                },
            },
        )
        assert manual_table.status_code == 200, manual_table.text
        proposal_view = manual_table.json()
        proposal_id = UUID(proposal_view["id"])
        proposal = None
        async with uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence(
                seeded.subject.id, anchor_id, proposal_id
            )
        assert proposal is not None
        assert proposal.origin is EditorialProposalOrigin.MANUAL_PATCH
        assert proposal.actor_id == uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst")
        assert proposal.thread_id is None
        assert proposal.model_run_id is None
        assert proposal.model_turn_id is None
        accepted = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/threads/{anchor_id}/"
            f"proposals/{proposal_id}/accept",
            headers={"Idempotency-Key": f"accept-table-{uuid4().hex}"},
            json={
                "expected_state_version": proposal.state_version,
                "patch_sha256": proposal.patch_sha256,
            },
        )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == EditorialProposalState.ACCEPTED.value
    assert model_gateway.calls == []

    async with uow_factory() as uow:
        stored_proposal = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, anchor_id, proposal_id
        )
        current = await uow.production_artifacts.get_current(
            seeded.run.id,
            ProductionArtifactStage.EDITORIAL_ENRICHMENT.value,
        )
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
        persisted_snapshot = await uow.editorial_workspace_snapshots.get_by_id(
            UUID(accepted.json()["snapshot_id"])
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal_id)
        canonical_blob = (
            await uow.blobs.get(current.canonical_blob_id)
            if current is not None and current.canonical_blob_id is not None
            else None
        )
    assert stored_proposal is not None
    assert stored_proposal.state is EditorialProposalState.ACCEPTED
    assert current is not None and current.version == 2
    assert [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ] == [1, 2]
    assert canonical_blob is not None
    assert persisted_snapshot is not None
    assert impact is not None and impact.accepted_snapshot_id == persisted_snapshot.snapshot_id
    assert impact.state.value == "pending"
    adoption_metadata = current.metadata.get("editorial_adoption")
    assert isinstance(adoption_metadata, dict)
    assert adoption_metadata["proposal_id"] == str(proposal_id)
    assert adoption_metadata["operation"] == "create_table"
    inserted_entry = next(
        entry
        for entry in persisted_snapshot.block_registry
        if entry.source_stage.value == "editorial_enrichment"
        and entry.structural_pointer == "/tables/1"
    )
    assert inserted_entry.position == 1
    anchor_entry = next(
        entry for entry in persisted_snapshot.block_registry if entry.block_id == anchor_id
    )

    enrichment_payload = json.loads(
        await seeded.blob_store.read(
            canonical_blob.descriptor, max_bytes=canonical_blob.descriptor.size
        )
    )
    enrichment = editorial_enrichment_from_json(enrichment_payload)
    from cti_app.application.publication_assembly import _resolve_editorial_placements

    placements = _resolve_editorial_placements(
        record=persisted_snapshot,
        run=seeded.run,
        synthesis=seeded.synthesis,
        enrichment=enrichment,
        enrichment_artifact_id=current.id,
    )
    inserted = next(item for item in enrichment.tables if item.key == table_key)
    assert len(placements) == 1
    assert placements[0].element_key == inserted.key
    assert placements[0].anchor_block_id == anchor_id
    assert placements[0].anchor_pointer == anchor_entry.structural_pointer

    # The publication assembly resolver feeds these exact placements into V9.
    # A compact corpus fixture keeps this PostgreSQL oracle independent of providers.
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
    normalized_reference_hash = references_corpus_hash(references)
    references = replace(
        references,
        _parsed_payload_sha256=seeded.extraction.references_corpus_hash,
        _parsed_normalized_payload_sha256=normalized_reference_hash,
    )
    publication_service, catalog, _ = _publication_service()
    assembly = await _assemble(
        publication_service,
        run=seeded.run,
        snapshot=seeded.production_input,
        references=references,
        extraction=seeded.extraction,
        synthesis=seeded.synthesis,
        editorial_enrichment=enrichment,
        publication_kind=PublicationKind.ARTICLE,
        editorial_enrichment_artifact_id=current.id,
        editorial_enrichment_artifact_input_hash=current.input_hash,
        workspace_snapshot_record=persisted_snapshot,
        routing_decision_hash="7" * 64,
        routing_policy_version="synthetic-routing-policy",
    )
    assert assembly.artifact.canonical_blob_id is not None
    document = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    assert isinstance(document, PublicationDocumentV9)
    assert any(table.key == inserted.key for table in document.tables)
    published_placement = next(
        item for item in document.resolved_placements if item.element_key == inserted.key
    )
    assert published_placement.anchor_block_id == anchor_id
    assert published_placement.anchor_pointer == anchor_entry.structural_pointer

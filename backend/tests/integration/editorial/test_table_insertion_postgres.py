from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from cti_app.application.editorial_proposal_adoption import (
    EnrichmentCandidateInputs as EnrichmentInputs,
)
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    registry_hash,
)
from cti_app.application.editorial_workspace.service import advance_workspace_registry
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    editorial_enrichment_evidence_pack_hash,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalOrigin,
    EditorialProposalState,
    ManualPatch,
    ManualTablePatch,
)
from cti_app.domain.editorial_workspace import (
    EditorialSourceStage,
    bootstrap_block_registry,
)
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_document import PublicationDocumentV9, parse_publication_document
from cti_app.domain.publication_kind import PublicationKind
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _PATCH_BUCKET,
    _PATCH_MIME,
    _enrichment_seed_content,
    _EnrichmentInputs,
    _seed_enrichment_proposal,
    _Seeded,
    _service,
    _table_proposal,
    _Workspace,
)
from tests.test_publication_assembly import _assemble
from tests.test_publication_assembly import _service as _publication_service

pytestmark = pytest.mark.integration


class _TableWorkspace(_Workspace):
    def advance(
        self,
        *,
        base_registry,
        artifact_id,
        canonical_sha256,
        source_stage,
        canonical_content,
        lineage,
        retired_block_ids=(),
    ):
        return advance_workspace_registry(
            base_registry=base_registry,
            artifact_id=artifact_id,
            canonical_sha256=canonical_sha256,
            source_stage=source_stage,
            canonical_content=canonical_content,
            lineage=lineage,
            retired_block_ids=retired_block_ids,
        )


async def _manual_table_insertion_seed(uow_factory, *, seeded: _Seeded) -> _Seeded:
    enrichment_content = _enrichment_seed_content(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
    )
    synthesis_artifact_id = seeded.snapshot.synthesis_artifact_id
    synthesis_payload = production_synthesis_to_json(seeded.synthesis)
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(synthesis_payload)
    ).hexdigest()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=synthesis_sha256,
        source_stage=EditorialSourceStage.SYNTHESIS,
        canonical_content=synthesis_payload,
    )
    enrichment_entries = bootstrap_block_registry(
        artifact_id=seeded.base_artifact.id,
        canonical_sha256=enrichment_content.base_sha256,
        source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
        canonical_content=enrichment_content.base_payload,
    )
    entries = (*synthesis_entries, *enrichment_entries)
    anchor = next(
        entry for entry in synthesis_entries if entry.kind.value in {"paragraph", "lead_paragraph"}
    )
    record = replace(
        seeded.snapshot,
        snapshot_id=uuid4(),
        workspace_pin_hash="9" * 64,
        registry_hash=registry_hash(entries),
        block_registry=entries,
        parent_snapshot_id=seeded.snapshot.snapshot_id,
    )
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
        None,
        workspace_record=record,
    )
    evidence_handle = next(
        str(item["handle"])
        for item in evidence_pack.narrative_evidence
        if item.get("value") == "Synthetic finding"
    )
    table_key = "table_inserted_after_registered_paragraph"
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor.block_id,
    )
    typed_table = replace(
        enrichment_content.base_enrichment.tables[0],
        key=table_key,
        title="Inserted synthetic findings",
        placement=placement,
    )
    manual_patch = ManualPatch(
        typed_patch=ManualTablePatch(anchor_block_id=anchor.block_id, table=typed_table)
    )
    proposal_wire = _table_proposal(evidence_handle, title=typed_table.title)
    wire_table = proposal_wire.tables[0].model_copy(
        update={
            "key": manual_patch.typed_patch.table.key,
            "placement": {
                "kind": EnrichmentPlacementKind.AFTER_PARAGRAPH,
                "anchor_block_id": manual_patch.typed_patch.anchor_block_id,
            },
        }
    )
    proposal_wire = proposal_wire.model_copy(update={"tables": (wire_table,)})
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key=None,
        placement=TargetedEditorialRevisionPlacement(
            kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor.block_id,
        ),
    )
    operation = TargetedEditorialRevisionOperation.CREATE_TABLE
    target_id = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(target.to_json())
    ).hexdigest()
    patch_payload = {
        "schema_version": "editorial-patch-v1",
        "operation": operation.value,
        "target": target.to_json(),
        "target_id": target_id,
        "occurrence_id": str(anchor.block_id),
        "before_hash": anchor.content_sha256,
        "enrichment_patch": proposal_wire.model_dump(mode="json"),
        "rationale": "Manual typed table insertion at the registered paragraph.",
    }
    patch_bytes = ProductionArtifactStore.canonical_json_bytes(patch_payload)
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    patch_descriptor = BlobDescriptor(
        patch_sha256,
        len(patch_bytes),
        _PATCH_MIME,
        _PATCH_BUCKET,
    )
    seeded.blob_store.seed(patch_descriptor, patch_bytes)
    patch_blob = BlobRecord(descriptor=patch_descriptor)
    proposal_id = uuid4()
    proposal = EditorialProposal(
        id=proposal_id,
        subject_id=seeded.subject.id,
        production_run_id=seeded.run.id,
        origin=EditorialProposalOrigin.MANUAL_PATCH,
        snapshot_id=record.snapshot_id,
        occurrence_id=anchor.block_id,
        base_artifact_id=seeded.base_artifact.id,
        base_artifact_version=seeded.base_artifact.version,
        base_input_hash=seeded.base_artifact.input_hash,
        base_canonical_sha256=enrichment_content.base_sha256,
        base_routing_hash=record.routing_decision_hash,
        base_pin_hash=record.workspace_pin_hash,
        target_id=target_id,
        operation=operation.value,
        before_hash=anchor.content_sha256,
        patch_blob_ref=patch_blob.id,
        patch_sha256=patch_sha256,
        evidence_refs=(),
        diagnostics={},
        thread_id=None,
        variant_group_id=uuid4(),
        model_run_id=None,
        model_turn_id=None,
        state=EditorialProposalState.PROPOSED,
        state_version=1,
        actor_id=seeded.actor_id,
        idempotency_key=f"manual-table-{uuid4().hex}",
        request_sha256=hashlib.sha256(patch_bytes).hexdigest(),
    )
    event = EditorialProposalEvent(
        id=uuid4(),
        proposal_id=proposal.id,
        sequence=1,
        kind=EditorialProposalEventKind.PREPARED,
        state=EditorialProposalState.PROPOSED,
        state_version=1,
        actor_id=proposal.actor_id,
        idempotency_key=proposal.idempotency_key,
        request_sha256=proposal.request_sha256,
        patch_sha256=patch_sha256,
    )
    async with uow_factory() as uow:
        await uow.editorial_workspace_snapshots.append_if_absent(record)
        stored_blob = await uow.blobs.add_if_absent(patch_blob)
        proposal = replace(proposal, patch_blob_ref=stored_blob.id)
        stored, inserted = await uow.editorial_proposals.add_if_absent(
            proposal, event=event, model_run=None
        )
        assert inserted
        await uow.commit()

    resolution = WorkspaceOccurrenceResolution(
        record=record,
        entry=anchor,
        binding_state="valid",
        artifact_id=synthesis_artifact_id,
        artifact_version=record.synthesis_version,
        canonical_sha256=record.synthesis_canonical_sha256,
    )
    candidate_payload = editorial_enrichment_to_json(
        replace(
            enrichment_content.base_enrichment,
            tables=(*enrichment_content.base_enrichment.tables, typed_table),
        )
    )
    lineage = ProductionEditorialProposalAdoptionInputs._enrichment_lineage(
        resolution,
        enrichment_content.base_payload,
        candidate_payload,
    )
    candidate_inputs = EnrichmentInputs(
        base_payload=enrichment_content.base_payload,
        base_enrichment=enrichment_content.base_enrichment,
        extraction=seeded.extraction,
        synthesis=seeded.synthesis,
        evidence_pack=evidence_pack,
        targeted_request=TargetedEditorialRevisionRequest(
            subject_id=seeded.subject.id,
            base_artifact_id=seeded.base_artifact.id,
            base_version=seeded.base_artifact.version,
            base_input_hash=seeded.base_artifact.input_hash,
            base_canonical_sha256=enrichment_content.base_sha256,
            target=target,
            operation=operation,
            instruction="Manual typed table insertion",
            mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
            requester=str(seeded.actor_id),
            before_hash=anchor.content_sha256,
        ),
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key=table_key,
        input_hash=seeded.base_artifact.input_hash,
        artifact_id=uuid4(),
        registry_lineage=lineage,
        is_create=True,
        evidence_pack_hash=editorial_enrichment_evidence_pack_hash(evidence_pack),
        access_policy_hash="e" * 64,
        model_policy_version="synthetic-enrichment-policy",
        routing_policy_version="synthetic-routing-policy",
    )
    return replace(
        seeded,
        snapshot=record,
        proposals=(stored,),
        patch_sha256=patch_sha256,
        workspace=_TableWorkspace(resolution),
        inputs=_EnrichmentInputs(seeded.inputs.synthesis, candidate_inputs),
    )


@pytest.mark.asyncio
async def test_manual_table_patch_accept_publishes_table_at_registered_anchor(
    uow_factory,
) -> None:
    seeded = await _seed_enrichment_proposal(uow_factory)
    seeded = await _manual_table_insertion_seed(uow_factory, seeded=seeded)
    proposal = seeded.proposals[0]

    adopted = await _service(seeded, uow_factory).adopt(
        subject_id=proposal.subject_id,
        occurrence_id=proposal.occurrence_id,
        proposal_id=proposal.id,
        actor_id=seeded.actor_id,
        expected_state_version=proposal.state_version,
        patch_sha256=proposal.patch_sha256,
        idempotency_key=f"accept-table-{uuid4().hex}",
    )

    assert adopted.proposal.state is EditorialProposalState.ACCEPTED
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current(
            proposal.production_run_id,
            ProductionArtifactStage.EDITORIAL_ENRICHMENT.value,
        )
        canonical_blob = (
            await uow.blobs.get(current.canonical_blob_id)
            if current is not None and current.canonical_blob_id is not None
            else None
        )
    assert current is not None and canonical_blob is not None
    enrichment_payload = json.loads(
        await seeded.blob_store.read(
            canonical_blob.descriptor, max_bytes=canonical_blob.descriptor.size
        )
    )
    enrichment = editorial_enrichment_from_json(enrichment_payload)

    from cti_app.application.publication_assembly import _resolve_editorial_placements

    placements = _resolve_editorial_placements(
        record=adopted.snapshot,
        run=seeded.run,
        synthesis=seeded.synthesis,
        enrichment=enrichment,
        enrichment_artifact_id=adopted.artifact.id,
    )
    inserted = next(
        item
        for item in enrichment.tables
        if item.key == "table_inserted_after_registered_paragraph"
    )
    assert len(placements) == 1
    assert placements[0].element_key == inserted.key
    assert placements[0].anchor_block_id == proposal.occurrence_id

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
        editorial_enrichment_artifact_id=adopted.artifact.id,
        editorial_enrichment_artifact_input_hash=adopted.artifact.input_hash,
        workspace_snapshot_record=adopted.snapshot,
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
    assert published_placement.anchor_block_id == proposal.occurrence_id

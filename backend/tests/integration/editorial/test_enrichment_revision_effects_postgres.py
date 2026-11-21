from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionError
from cti_app.application.editorial_revision_effects import EditorialRevisionEffects
from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    ProductionEditorialEnrichmentService,
    build_editorial_enrichment_evidence_pack,
    build_editorial_figure_catalog,
    build_source_figure_provenance_diagnostics,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
)
from cti_app.application.production_enrichment_revision import (
    EditorialEnrichmentRevisionConflictError,
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    synthesis_access_policy_hash,
)
from cti_app.application.production_synthesis_revision import (
    TargetedEditorialRevisionConflictError,
)
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.semantic_annotation import SemanticAnnotationProposalV1, SemanticRole
from tests.integration.editorial.test_editorial_revision_effects_postgres import (
    _CurrentWorkspace,
    _snapshots,
    _stage_rows,
)
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _adopt_kwargs,
    _enrichment_seed_content,
    _seed,
    _Seeded,
    _service,
)

pytestmark = pytest.mark.integration


class _UnexpectedModelGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def draft(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("delete and restore enrichment revisions must not call a model")

    async def get_run(self, _run_id: UUID) -> None:
        return None


async def _persist_enrichment_history(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
) -> tuple[ProductionArtifactStore, ProductionArtifact, str]:
    artifact_store = ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory))
    writer = EditorialEnrichmentService(uow_factory, artifact_store)
    content = _enrichment_seed_content(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
    )
    async with uow_factory() as uow:
        workspace_record = await EditorialWorkspaceService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
        ).snapshot_record_in_uow(uow, seeded.subject.id)
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
            source_media_repository=uow.source_media_candidates,
        )
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
        workspace_record=workspace_record,
    )
    evidence_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    access_hash = synthesis_access_policy_hash(access_policy)
    inventory_hash = inventory.functional_hash()
    input_hash = compute_editorial_enrichment_input_hash(
        extraction=seeded.extraction,
        synthesis=seeded.synthesis,
        evidence_pack_hash=evidence_hash,
        access_policy_hash=access_hash,
        source_figure_inventory_hash=inventory_hash,
        resource_search_enabled=False,
    )
    assert content.base_enrichment.tables[0].rows[0].evidence_refs[0] in (
        set(evidence_pack._handle_to_ref.values())
    )
    annotation = SemanticAnnotationProposalV1(
        role=SemanticRole.TECHNICAL,
        paragraph_anchor="section:0:paragraph:0001",
        text=seeded.synthesis.sections[0].paragraphs[0].text,
    )
    source_enrichment = replace(content.base_enrichment, annotations=(annotation,))
    current_enrichment = content.base_enrichment
    figure_catalog = build_editorial_figure_catalog(seeded.extraction, inventory)

    async def persist(
        enrichment: EditorialEnrichmentV1,
        *,
        revision: dict[str, Any] | None = None,
    ) -> ProductionArtifact:
        async with uow_factory() as uow:
            artifact = await writer.store_editorial_enrichment_result(
                run_id=seeded.run.id,
                subject_id=seeded.subject.id,
                input_hash=input_hash,
                enrichment=enrichment,
                extraction=seeded.extraction,
                synthesis=seeded.synthesis,
                raw_result=None,
                model_run_id=None,
                evidence_pack_hash=evidence_hash,
                access_policy_hash=access_hash,
                model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
                routing_policy_version="synthetic-routing-policy",
                projection_hash=None,
                source_figure_inventory_hash=inventory_hash,
                metadata_extra={
                    "source_figure_inventory_hash": inventory_hash,
                    "source_figure_provenance_diagnostics": (
                        build_source_figure_provenance_diagnostics(
                            inventory, figure_catalog, enrichment
                        )
                    ),
                    **({"revision": revision} if revision is not None else {}),
                },
                canonical_payload=editorial_enrichment_to_json(enrichment),
                invalidate_downstream=False,
                resource_search_enabled=False,
                uow=uow,
            )
            await uow.commit()
        return artifact

    source_artifact = await persist(source_enrichment)
    current_artifact = await persist(
        current_enrichment,
        revision={"base_artifact_id": str(source_artifact.id)},
    )
    return artifact_store, current_artifact, input_hash


async def _revision_service(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
) -> tuple[ProductionEditorialEnrichmentRevisionService, _UnexpectedModelGateway]:
    workspace = _CurrentWorkspace(uow_factory)
    effects = EditorialRevisionEffects(workspace_service=workspace)
    writer = EditorialEnrichmentService(uow_factory, artifact_store)
    gateway = _UnexpectedModelGateway()
    enrichment_service = ProductionEditorialEnrichmentService(
        uow_factory=cast(Any, uow_factory),
        artifact_store=artifact_store,
        model_gateway=cast(Any, gateway),
        editorial_enrichment_service=writer,
        resource_search_enabled=False,
    )
    revision_service = ProductionEditorialEnrichmentRevisionService(
        enrichment_service=enrichment_service,
        persistence_service=writer,
        publication_render_service=cast(Any, SimpleNamespace()),
        revision_effects=effects,
    )

    async def skip_assembly(**_kwargs: Any) -> None:
        return None

    revision_service._assemble_and_render = skip_assembly  # type: ignore[method-assign]
    return revision_service, gateway


def _targeted_delete_request(
    seeded: _Seeded,
    artifact: ProductionArtifact,
    input_hash: str,
    canonical_sha256: str,
) -> TargetedEditorialRevisionRequest:
    return TargetedEditorialRevisionRequest(
        subject_id=seeded.subject.id,
        base_artifact_id=artifact.id,
        base_version=artifact.version,
        base_input_hash=input_hash,
        base_canonical_sha256=canonical_sha256,
        target=TargetedEditorialRevisionTarget(
            kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key="synthetic_table",
        ),
        operation=TargetedEditorialRevisionOperation.DELETE_TABLE,
        instruction="Remove the selected table.",
        mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
        requester="dev-analyst",
    )


@pytest.mark.parametrize("writer_kind", ["revise", "restore"])
@pytest.mark.asyncio
async def test_accept_races_real_enrichment_revise_or_restore_with_one_winner_and_snapshot(
    uow_factory: UnitOfWorkFactory,
    writer_kind: str,
) -> None:
    seeded = await _seed(
        uow_factory,
        proposal_count=1,
        seed_discovery_lineage=True,
        article_base=True,
    )
    artifact_store, current_enrichment, input_hash = await _persist_enrichment_history(
        seeded, uow_factory
    )
    seeded = replace(seeded, artifact_store=artifact_store)
    proposal = seeded.proposals[0]
    adoption_service = _service(seeded, uow_factory)
    revision_service, gateway = await _revision_service(uow_factory, artifact_store)
    canonical_bytes = await artifact_store.read_bytes(
        cast(UUID, current_enrichment.canonical_blob_id)
    )
    base_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    snapshots_before = await _snapshots(uow_factory, seeded.run.id)
    synthesis_before = await _stage_rows(
        uow_factory, seeded.run.id, ProductionArtifactStage.SYNTHESIS
    )
    enrichment_before = await _stage_rows(
        uow_factory, seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT
    )
    assert len(snapshots_before) == 1
    assert len(synthesis_before) == 1
    assert len(enrichment_before) == 2

    if writer_kind == "revise":
        enrichment_write = revision_service.revise_targeted(
            _targeted_delete_request(
                seeded,
                current_enrichment,
                input_hash,
                base_sha256,
            )
        )
    else:
        enrichment_write = revision_service.restore_stale_enrichment_blocks(
            subject_id=seeded.subject.id,
            base_artifact_id=current_enrichment.id,
            base_version=current_enrichment.version,
            base_input_hash=input_hash,
            base_canonical_sha256=base_sha256,
            actor="dev-analyst",
        )

    results = await asyncio.gather(
        adoption_service.adopt(
            **_adopt_kwargs(seeded, proposal, f"accept-{writer_kind}-{uuid4().hex}")
        ),
        enrichment_write,
        return_exceptions=True,
    )

    winners = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(failures) == 1
    assert isinstance(
        failures[0],
        (
            EditorialProposalAdoptionError,
            EditorialEnrichmentRevisionConflictError,
            TargetedEditorialRevisionConflictError,
        ),
    )
    assert gateway.calls == 0

    snapshots_after = await _snapshots(uow_factory, seeded.run.id)
    synthesis_after = await _stage_rows(
        uow_factory, seeded.run.id, ProductionArtifactStage.SYNTHESIS
    )
    enrichment_after = await _stage_rows(
        uow_factory, seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT
    )
    assert len(snapshots_after) == len(snapshots_before) + 1
    assert snapshots_after[-1].parent_snapshot_id == snapshots_before[-1].snapshot_id
    async with uow_factory() as uow:
        current_proposal = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
    assert current_proposal is not None
    latest = snapshots_after[-1]
    if current_proposal.state is EditorialProposalState.ACCEPTED:
        assert len(synthesis_after) == len(synthesis_before) + 1
        assert len(enrichment_after) == len(enrichment_before)
        assert impact is not None
        assert impact.accepted_snapshot_id == latest.snapshot_id
        assert (
            latest.synthesis_artifact_id == max(synthesis_after, key=lambda item: item.version).id
        )
    else:
        assert current_proposal.state is EditorialProposalState.STALE
        assert len(synthesis_after) == len(synthesis_before)
        assert len(enrichment_after) == len(enrichment_before) + 1
        assert impact is None
        assert (
            latest.enrichment_artifact_id == max(enrichment_after, key=lambda item: item.version).id
        )

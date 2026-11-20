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
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    canonical_synthesis_hash,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
)
from cti_app.application.production_enrichment_revision import (
    EditorialEnrichmentRevisionConflictError,
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.application.production_synthesis import (
    SynthesisAccessPolicyV1,
    SynthesisAccessSourceV1,
    canonical_extraction_hash,
)
from cti_app.application.production_synthesis_revision import (
    TargetedEditorialRevisionConflictError,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_V3_POLICY_VERSION,
    EditorialEnrichmentV1,
    editorial_enrichment_to_json,
)
from cti_app.domain.semantic_annotation import SemanticAnnotationProposalV1, SemanticRole
from tests.integration.editorial.test_editorial_revision_effects_postgres import (
    _CurrentWorkspace,
    _revision_service,
)
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _adopt_kwargs,
    _seed,
    _Seeded,
    _service,
)

pytestmark = pytest.mark.integration


async def _stage_rows(
    uow_factory: UnitOfWorkFactory,
    run_id: UUID,
    stage: ProductionArtifactStage,
) -> tuple[ProductionArtifact, ...]:
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(run_id)
    return tuple(item for item in artifacts if item.stage is stage)


async def _snapshots(uow_factory: UnitOfWorkFactory, run_id: UUID) -> tuple[Any, ...]:
    async with uow_factory() as uow:
        return tuple(await uow.editorial_workspace_snapshots.list_for_run(run_id))


@pytest.mark.asyncio
async def test_accept_races_real_synthesis_revise_with_one_writer_and_one_snapshot(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(
        uow_factory,
        proposal_count=1,
        seed_discovery_lineage=True,
        article_base=True,
    )
    proposal = seeded.proposals[0]
    revision_service, request, gateway, _store, workspace = await _revision_service(
        seeded, uow_factory
    )
    seeded = replace(seeded, workspace=workspace)
    adoption_service = _service(seeded, uow_factory)
    snapshots_before = await _snapshots(uow_factory, seeded.run.id)
    synthesis_before = await _stage_rows(
        uow_factory, seeded.run.id, ProductionArtifactStage.SYNTHESIS
    )
    assert len(snapshots_before) == 1
    assert len(synthesis_before) == 1

    results = await asyncio.gather(
        adoption_service.adopt(**_adopt_kwargs(seeded, proposal, f"accept-{uuid4().hex}")),
        revision_service.revise(request),
        return_exceptions=True,
    )

    winners = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(failures) == 1
    assert isinstance(
        failures[0], (EditorialProposalAdoptionError, TargetedEditorialRevisionConflictError)
    )
    assert gateway.calls == 1

    snapshots_after = await _snapshots(uow_factory, seeded.run.id)
    synthesis_after = await _stage_rows(
        uow_factory, seeded.run.id, ProductionArtifactStage.SYNTHESIS
    )
    assert len(snapshots_after) == len(snapshots_before) + 1
    assert snapshots_after[-1].parent_snapshot_id == snapshots_before[-1].snapshot_id
    assert len(synthesis_after) == len(synthesis_before) + 1
    latest = snapshots_after[-1]
    assert latest.synthesis_artifact_id == max(synthesis_after, key=lambda item: item.version).id

    async with uow_factory() as uow:
        current_proposal = await uow.editorial_proposals.get_for_occurrence(
            proposal.subject_id, proposal.occurrence_id, proposal.id
        )
        impact = await uow.editorial_proposal_impacts.get_by_proposal(proposal.id)
    assert current_proposal is not None
    if current_proposal.state is EditorialProposalState.ACCEPTED:
        assert impact is not None
        assert impact.accepted_snapshot_id == latest.snapshot_id
    else:
        assert current_proposal.state is EditorialProposalState.STALE
        assert impact is None


async def _persist_enrichment_history(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
) -> tuple[ProductionArtifactStore, ProductionArtifact, str, Any]:
    artifact_store = ProductionArtifactStore(BlobCatalogService(seeded.blob_store, uow_factory))
    writer = EditorialEnrichmentService(uow_factory, artifact_store)
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
        None,
    )
    evidence_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    access_hash = "e" * 64
    input_hash = compute_editorial_enrichment_input_hash(
        extraction=seeded.extraction,
        synthesis=seeded.synthesis,
        evidence_pack_hash=evidence_hash,
        access_policy_hash=access_hash,
        projection_hash=None,
        source_figure_inventory_hash=None,
    )
    annotation = SemanticAnnotationProposalV1(
        role=SemanticRole.TECHNICAL,
        paragraph_anchor="section:0:paragraph:0001",
        text=seeded.synthesis.sections[0].paragraphs[0].text,
    )
    source_enrichment = EditorialEnrichmentV1(
        schema_version=3,
        subject_id=seeded.subject.id,
        production_input_hash=seeded.production_input.input_hash,
        extraction_hash=canonical_extraction_hash(seeded.extraction),
        synthesis_hash=canonical_synthesis_hash(seeded.synthesis),
        publication_language=seeded.production_input.publication_language,
        enrichment_policy_version=EDITORIAL_ENRICHMENT_V3_POLICY_VERSION,
        tables=(),
        diagrams=(),
        charts=(),
        source_figures=(),
        annotations=(annotation,),
        warnings=(),
    )
    current_enrichment = replace(source_enrichment, annotations=())

    async def persist(
        enrichment: EditorialEnrichmentV1,
        *,
        metadata_extra: dict[str, Any],
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
                model_policy_version="synthetic-enrichment-policy",
                routing_policy_version="synthetic-routing-policy",
                projection_hash=None,
                source_figure_inventory_hash=None,
                metadata_extra=metadata_extra,
                canonical_payload=editorial_enrichment_to_json(enrichment),
                invalidate_downstream=False,
                uow=uow,
            )
            await uow.commit()
        return artifact

    source_artifact = await persist(
        source_enrichment,
        metadata_extra={"source_figure_inventory_hash": None},
    )
    current_artifact = await persist(
        current_enrichment,
        metadata_extra={
            "source_figure_inventory_hash": None,
            "revision": {"base_artifact_id": str(source_artifact.id)},
        },
    )
    access_policy = SynthesisAccessPolicyV1(
        subject_tlp=TLP.AMBER,
        effective_tlp=TLP.AMBER,
        external_llm_allowed=True,
        do_not_submit=False,
        sources=tuple(
            SynthesisAccessSourceV1(
                source_document_id=source.source_document_id,
                tlp=TLP.AMBER,
                external_llm_allowed=True,
                do_not_submit=False,
            )
            for source in seeded.extraction.sources
        ),
    )
    return (
        artifact_store,
        current_artifact,
        input_hash,
        SimpleNamespace(
            evidence_pack=evidence_pack,
            evidence_pack_hash=evidence_hash,
            input_hash=input_hash,
            access_policy=access_policy,
            inventory=None,
            inventory_hash=None,
            figure_catalog=(),
        ),
    )


@pytest.mark.asyncio
async def test_accept_races_real_enrichment_restore_with_one_writer_and_one_snapshot(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(
        uow_factory,
        proposal_count=1,
        seed_discovery_lineage=True,
        article_base=True,
    )
    artifact_store, current_enrichment, input_hash, fence = await _persist_enrichment_history(
        seeded, uow_factory
    )
    seeded = replace(seeded, artifact_store=artifact_store)
    proposal = seeded.proposals[0]
    adoption_service = _service(seeded, uow_factory)
    workspace = _CurrentWorkspace(uow_factory)
    effects = EditorialRevisionEffects(workspace_service=workspace)
    enrichment_writer = EditorialEnrichmentService(uow_factory, artifact_store)
    enrichment_service = SimpleNamespace(
        _uow_factory=uow_factory,
        _artifact_store=artifact_store,
        _resource_search_enabled=False,
    )
    revision_service = ProductionEditorialEnrichmentRevisionService(
        enrichment_service=cast(Any, enrichment_service),
        persistence_service=enrichment_writer,
        publication_render_service=cast(Any, SimpleNamespace()),
        revision_effects=effects,
    )

    async def load_context(**_kwargs: Any) -> tuple[Any, ...]:
        async with uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(seeded.subject.id)
            current = await uow.production_artifacts.get(current_enrichment.id)
            synthesis = await uow.production_artifacts.get(seeded.base_artifact.id)
        assert run is not None and current is not None and synthesis is not None
        payload = await artifact_store.read_json(cast(UUID, current.canonical_blob_id))
        refs_blob = await artifact_store.store_stage_payloads(canonical={"synthetic": "refs"})
        extraction_blob = await artifact_store.store_stage_payloads(
            canonical={"synthetic": "extraction"}
        )
        references = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash="a" * 64,
            canonical_blob_id=refs_blob[1],
        )
        extraction = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash=canonical_extraction_hash(seeded.extraction),
            canonical_blob_id=extraction_blob[1],
        )
        return (
            run,
            seeded.production_input,
            current,
            payload,
            current_enrichment,
            references,
            extraction,
            None,
            synthesis,
            None,
        )

    async def load_extraction(*_args: Any, **_kwargs: Any) -> Any:
        return seeded.extraction

    async def load_synthesis(*_args: Any, **_kwargs: Any) -> Any:
        return seeded.synthesis

    async def validate_input_fence(**_kwargs: Any) -> Any:
        return fence

    async def skip_assembly(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "cti_app.application.production_enrichment_revision.production_reference_corpus_from_json",
        lambda _payload: None,
    )
    monkeypatch.setattr(
        "cti_app.application.production_enrichment_revision.build_source_figure_provenance_diagnostics",
        lambda *_args: {},
    )
    revision_service._load_context = load_context  # type: ignore[method-assign]
    revision_service._validate_input_fence = validate_input_fence  # type: ignore[method-assign]
    revision_service._assemble_and_render = skip_assembly  # type: ignore[method-assign]
    enrichment_service._load_extraction = load_extraction  # type: ignore[attr-defined]
    enrichment_service._load_synthesis = load_synthesis  # type: ignore[attr-defined]
    current_bytes = await artifact_store.read_bytes(
        cast(UUID, current_enrichment.canonical_blob_id)
    )
    snapshots_before = await _snapshots(uow_factory, seeded.run.id)
    synthesis_before = await _stage_rows(
        uow_factory, seeded.run.id, ProductionArtifactStage.SYNTHESIS
    )
    enrichment_before = await _stage_rows(
        uow_factory, seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT
    )
    assert len(snapshots_before) == 1
    assert len(enrichment_before) == 2

    results = await asyncio.gather(
        adoption_service.adopt(**_adopt_kwargs(seeded, proposal, f"accept-{uuid4().hex}")),
        revision_service.restore_stale_enrichment_blocks(
            subject_id=seeded.subject.id,
            base_artifact_id=current_enrichment.id,
            base_version=current_enrichment.version,
            base_input_hash=input_hash,
            base_canonical_sha256=hashlib.sha256(current_bytes).hexdigest(),
            actor="dev-analyst",
        ),
        return_exceptions=True,
    )

    winners = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(failures) == 1
    assert isinstance(
        failures[0], (EditorialProposalAdoptionError, EditorialEnrichmentRevisionConflictError)
    )

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
    if current_proposal.state is EditorialProposalState.ACCEPTED:
        assert len(synthesis_after) == len(synthesis_before) + 1
        assert len(enrichment_after) == len(enrichment_before)
        assert impact is not None
        assert (
            snapshots_after[-1].synthesis_artifact_id
            == max(synthesis_after, key=lambda item: item.version).id
        )
    else:
        assert current_proposal.state is EditorialProposalState.STALE
        assert len(synthesis_after) == len(synthesis_before)
        assert len(enrichment_after) == len(enrichment_before) + 1
        assert impact is None
        assert (
            snapshots_after[-1].enrichment_artifact_id
            == max(enrichment_after, key=lambda item: item.version).id
        )

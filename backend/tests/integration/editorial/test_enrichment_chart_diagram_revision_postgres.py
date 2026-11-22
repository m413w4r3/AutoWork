"""PostgreSQL oracles for typed chart/diagram revisions (F2-03b).

The fixture starts from the shared manual-route seed (a table plus exact
quantitative facts), then persists one canonical enrichment version holding a
BAR chart and a flow diagram. Revisions go through prepare -> shared planner ->
adoption; adoption re-runs the planner under the run lock, recompiles the
chart/diagram asset and never calls a model.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_proposal_adoption import (
    EditorialProposalAdoptionError,
    EditorialProposalAdoptionResult,
    EditorialProposalAdoptionService,
)
from cti_app.application.editorial_proposal_adoption_inputs import (
    ProductionEditorialProposalAdoptionInputs,
)
from cti_app.application.editorial_proposals import (
    EditorialProposalApplicationError,
    EditorialProposalService,
)
from cti_app.application.editorial_visual_revision import VisualRevisionContextLoader
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    ProductionEditorialEnrichmentService,
)
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService, SynthesisService
from cti_app.application.production_synthesis import (
    ProductionSynthesisService,
    _all_evidence_entries,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editorial_enrichment_patch import (
    ReviseChartPatch,
    ReviseDiagramPatch,
)
from cti_app.domain.editorial_proposal import EditorialProposalState
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    DiagramEdgeV1,
    DiagramNodeV1,
    DiagramProfile,
    DiagramSpecV1,
    EditorialAnalyticPurposeV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
)
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from tests.integration.editorial.test_enrichment_visual_revision_postgres import (
    _adopt,
    _configure_app,
    _enrichment_block,
    _read_enrichment,
    _seed_ready_workspace_snapshot,
)
from tests.integration.editorial.test_proposal_adoption_postgres import _Seeded
from tests.integration.editorial.test_table_insertion_postgres import (
    _CANONICAL_BUCKET,
    _NoModelCalls,
    _ready_for_manual_table_route,
)

pytestmark = pytest.mark.integration

ALPHA_VALUE = "Alpha: 12 endpoints"
BETA_VALUE = "Beta: 18 endpoints"
BASE_CHART_ASSET = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BASE_DIAGRAM_ASSET = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _services(
    app: Any, uow_factory: Any
) -> tuple[Any, EditorialProposalService, EditorialProposalAdoptionService]:
    request = type("_Request", (), {"app": app})()
    workspace_service = editorial_workspace_api._service(request)
    artifact_store = app.state.production_artifact_store
    blob_store = app.state.blob_store
    model_gateway = _NoModelCalls()
    synthesis_writer = SynthesisService(uow_factory, artifact_store)
    enrichment_writer = EditorialEnrichmentService(uow_factory, artifact_store)
    collection_service = editorial_workspace_api._collection_service_for_request(request)
    media_store = MediaAssetStore(artifact_store, uow_factory)
    enrichment_loader = ProductionEditorialEnrichmentService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_gateway=model_gateway,
        editorial_enrichment_service=enrichment_writer,
        media_asset_store=media_store,
        diagram_compiler=D2DiagramCompiler(),
        chart_compiler=AnalyticChartRenderer(),
        source_media_archiver=getattr(collection_service, "source_media_archiver", None),
        resource_search_enabled=False,
    )
    revision_service = ProductionEditorialEnrichmentRevisionService(
        enrichment_service=enrichment_loader,
        persistence_service=enrichment_writer,
        publication_render_service=app.state.publication_render_service,
    )
    loader = VisualRevisionContextLoader(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        workspace_service=workspace_service,
        enrichment_loader=enrichment_loader,
        enrichment_revision_service=revision_service,
    )
    proposal_service = EditorialProposalService(
        uow_factory=uow_factory,
        blob_store=blob_store,
        artifact_store=artifact_store,
        workspace_service=workspace_service,
        chart_compiler=AnalyticChartRenderer(),
        media_asset_store=media_store,
        visual_revision_loader=loader,
        diagram_preview_compiler=D2DiagramCompiler(),
    )
    adoption_inputs = ProductionEditorialProposalAdoptionInputs(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        workspace_service=workspace_service,
        synthesis_loader=ProductionSynthesisService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            model_gateway=model_gateway,
            synthesis_service=synthesis_writer,
        ),
        enrichment_loader=enrichment_loader,
        enrichment_revision_service=revision_service,
        resource_search_enabled=False,
    )
    adoption_service = EditorialProposalAdoptionService(
        uow_factory=uow_factory,
        blob_store=blob_store,
        artifact_store=artifact_store,
        workspace_service=workspace_service,
        inputs=adoption_inputs,
        synthesis_writer=synthesis_writer,
        enrichment_writer=enrichment_writer,
    )
    return workspace_service, proposal_service, adoption_service


def _chart(seed: _Seeded, *, compiled_asset_id: UUID = BASE_CHART_ASSET) -> ChartSpecV1:
    entries = _all_evidence_entries(seed.extraction)
    refs = {
        str(payload.get("value")): ref
        for ref, payload in entries.items()
        if payload.get("value") in {ALPHA_VALUE, BETA_VALUE}
    }
    alpha_ref, beta_ref = refs[ALPHA_VALUE], refs[BETA_VALUE]
    return ChartSpecV1(
        key="synthetic_endpoint_counts",
        kind=ChartKind.BAR,
        title="Documented endpoint counts",
        caption="Synthetic endpoint counts from the admitted facts.",
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
        purpose=EditorialAnalyticPurposeV1(
            question="Which endpoint count is documented for each synthetic category?",
            available_data="Two exact counts are quoted for these categories.",
            comprehension_gain="The comparison exposes the documented difference.",
            scope="Only Alpha and Beta in this fixture.",
            evidence_refs=(alpha_ref, beta_ref),
            knowledge_limits="No other category is extrapolated.",
            placement_reason="After the synthetic lead.",
        ),
        points=(
            ChartPointV1("Alpha", None, "Endpoints", (alpha_ref,), value=Decimal("12")),
            ChartPointV1("Beta", None, "Endpoints", (beta_ref,), value=Decimal("18")),
        ),
        category_axis_label="Source record",
        value_axis_label="Documented count",
        unit="endpoints",
        compiled_asset_id=compiled_asset_id,
    )


def _diagram(seed: _Seeded, *, compiled_asset_id: UUID = BASE_DIAGRAM_ASSET) -> DiagramSpecV1:
    entries = _all_evidence_entries(seed.extraction)
    refs = {
        str(payload.get("value")): ref
        for ref, payload in entries.items()
        if payload.get("value") in {ALPHA_VALUE, BETA_VALUE}
    }
    alpha_ref, beta_ref = refs[ALPHA_VALUE], refs[BETA_VALUE]
    return DiagramSpecV1(
        key="synthetic_flow",
        kind=EnrichmentDiagramKind.INFECTION_CHAIN,
        profile=DiagramProfile.FLOW,
        title="Documented synthetic flow",
        caption="Synthetic flow from admitted facts.",
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("loader", "Alpha", (alpha_ref,)),
            DiagramNodeV1("payload", "Beta", (beta_ref,)),
        ),
        edges=(DiagramEdgeV1("loader", "payload", "loads", (alpha_ref,)),),
        groups=(),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        compiled_asset_id=compiled_asset_id,
        purpose=EditorialAnalyticPurposeV1(
            question="Which documented action links Alpha and Beta?",
            available_data="One exact association is documented.",
            comprehension_gain="The direction and label expose the documented link.",
            scope="Only the synthetic association.",
            evidence_refs=(alpha_ref,),
            knowledge_limits="No unstated relationship is inferred.",
            placement_reason="At the end of the article.",
        ),
    )


async def _seed_base_revision_artifact(
    *,
    app: Any,
    uow_factory: Any,
    seeded: _Seeded,
) -> UUID:
    """Persist a second enrichment version holding one chart and one diagram."""
    artifact_store = app.state.production_artifact_store
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
    assert current is not None and current.canonical_blob_id is not None
    payload = await artifact_store.read_json(current.canonical_blob_id)
    base = editorial_enrichment_from_json(payload)
    revised_payload = editorial_enrichment_to_json(
        replace(
            base,
            charts=(_chart(seeded),),
            diagrams=(_diagram(seeded),),
        )
    )
    content = ProductionArtifactStore.canonical_json_bytes(revised_payload)
    descriptor = BlobDescriptor(
        hashlib.sha256(content).hexdigest(), len(content), "application/json", _CANONICAL_BUCKET
    )
    seeded.blob_store.seed(descriptor, content)
    async with uow_factory() as uow:
        stored_blob = await uow.blobs.add_if_absent(BlobRecord(descriptor=descriptor))
        artifact = ProductionArtifact(
            production_run_id=seeded.run.id,
            subject_id=seeded.subject.id,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            version=current.version + 1,
            input_hash=current.input_hash,
            status=ProductionArtifactStatus.VERIFIED,
            metadata=dict(current.metadata),
            canonical_blob_id=stored_blob.id,
        )
        await uow.production_artifacts.append(artifact)
        await uow.commit()
    return artifact.id


async def _ready_artifact(
    uow_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    _Seeded,
    Any,
    _NoModelCalls,
    Any,
    EditorialProposalService,
    EditorialProposalAdoptionService,
]:
    _configure_app(monkeypatch)
    seeded, app, model_gateway, _content = await _ready_for_manual_table_route(
        uow_factory, bar_chart_facts=True
    )
    workspace_service, proposal_service, adoption_service = _services(app, uow_factory)
    await _seed_base_revision_artifact(app=app, uow_factory=uow_factory, seeded=seeded)
    await _seed_ready_workspace_snapshot(
        workspace_service=workspace_service,
        uow_factory=uow_factory,
        artifact_store=app.state.production_artifact_store,
        subject_id=seeded.subject.id,
    )
    return (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    )


async def _enrichment_versions(uow_factory: Any, run_id: UUID) -> list[int]:
    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(run_id)
    return [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ]


@pytest.mark.asyncio
async def test_chart_revision_reproves_value_unit_and_compiles_a_new_asset(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
    assert current is not None and current.canonical_blob_id is not None
    base_enrichment = editorial_enrichment_from_json(
        await app.state.production_artifact_store.read_json(current.canonical_blob_id)
    )
    base_chart = base_enrichment.charts[0]
    alpha_ref = base_chart.points[0].evidence_refs[0]
    revised = replace(
        base_chart,
        title="Revised documented endpoint counts",
        caption="Revised synthetic caption.",
        points=(
            replace(base_chart.points[0], label="Alpha (revised)"),
            # A changed value must be re-proven by an admitted record: the
            # Alpha FACT documents exactly "12 endpoints".
            replace(
                base_chart.points[1],
                label="Beta (revised)",
                value=Decimal("12"),
                evidence_refs=(alpha_ref,),
            ),
        ),
        purpose=replace(base_chart.purpose, evidence_refs=(alpha_ref,)),
    )
    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"revise-chart-{uuid4().hex}",
        patch=ReviseChartPatch(element_key=chart_block["element_key"], chart=revised),
    )
    preview = cast(dict[str, object], proposal.diagnostics["preview"])
    chart_asset_preview = cast(dict[str, object], preview["chart_asset"])
    assert chart_asset_preview["asset_id"] != str(BASE_CHART_ASSET)

    result = await _adopt(
        adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
    )
    assert result.artifact.version == 3
    assert result.impact.state.value == "pending"

    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
        stored = await uow.editorial_proposals.get_for_occurrence(
            seeded.subject.id, proposal.occurrence_id, proposal.id
        )
    enrichment_versions = [
        artifact.version
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ]
    assert enrichment_versions == [1, 2, 3]
    assert stored is not None and stored.state is EditorialProposalState.ACCEPTED
    assert model_gateway.calls == []
    assert result.proposal.model_run_id is None

    version_two = next(
        artifact
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
        and artifact.version == 2
        and artifact.canonical_blob_id is not None
    )
    version_two_payload = await app.state.production_artifact_store.read_json(
        version_two.canonical_blob_id
    )
    assert (
        editorial_enrichment_from_json(version_two_payload).charts[0].compiled_asset_id
        == BASE_CHART_ASSET
    )

    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    enrichment = editorial_enrichment_from_json(payload)
    assert len(enrichment.charts) == 1
    chart = enrichment.charts[0]
    assert chart.key == base_chart.key
    assert chart.title == "Revised documented endpoint counts"
    assert [point.value for point in chart.points] == [Decimal("12"), Decimal("12")]
    assert chart.compiled_asset_id is not None
    assert chart.compiled_asset_id != BASE_CHART_ASSET
    async with uow_factory() as uow:
        manifest = await uow.media_assets.get(chart.compiled_asset_id)
    assert manifest is not None and manifest.kind is MediaAssetKind.CHART_SVG


@pytest.mark.asyncio
async def test_chart_value_without_admitted_proof_is_refused_without_a_canonical_write(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        _adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
    assert current is not None and current.canonical_blob_id is not None
    base_chart = editorial_enrichment_from_json(
        await app.state.production_artifact_store.read_json(current.canonical_blob_id)
    ).charts[0]
    unproven = replace(
        base_chart,
        title="Unproven revision",
        points=(base_chart.points[0], replace(base_chart.points[1], value=Decimal("21"))),
    )
    with pytest.raises(EditorialProposalApplicationError) as refusal:
        await proposal_service.prepare_visual_revision(
            subject_id=seeded.subject.id,
            snapshot_id=UUID(snapshot["snapshot_id"]),
            occurrence_id=UUID(chart_block["block_id"]),
            actor_id=seeded.actor_id,
            idempotency_key=f"revise-chart-refused-{uuid4().hex}",
            patch=ReviseChartPatch(element_key=chart_block["element_key"], chart=unproven),
        )
    assert refusal.value.code == "bar_chart_value_not_evidenced"
    assert await _enrichment_versions(uow_factory, seeded.run.id) == [1, 2]
    assert model_gateway.calls == []


@pytest.mark.asyncio
async def test_diagram_revision_recompiles_and_retains_the_old_asset(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    diagram_block = _enrichment_block(snapshot, kind="diagram")
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
    assert current is not None and current.canonical_blob_id is not None
    base_diagram = editorial_enrichment_from_json(
        await app.state.production_artifact_store.read_json(current.canonical_blob_id)
    ).diagrams[0]
    revised = replace(
        base_diagram,
        title="Revised synthetic flow",
        caption="Revised synthetic flow caption.",
        nodes=(
            replace(base_diagram.nodes[0], label="Alpha loader"),
            replace(base_diagram.nodes[1], label="Beta payload"),
        ),
        edges=(replace(base_diagram.edges[0], label="transfers to"),),
    )
    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(diagram_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"revise-diagram-{uuid4().hex}",
        patch=ReviseDiagramPatch(element_key=diagram_block["element_key"], diagram=revised),
    )
    preview = cast(dict[str, object], proposal.diagnostics["preview"])
    diagram_preview = cast(dict[str, object], preview["diagram"])
    assert diagram_preview["media_type"] == "image/png"
    assert diagram_preview["byte_size"] != 0

    result = await _adopt(
        adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
    )
    assert result.artifact.version == 3
    assert model_gateway.calls == []

    async with uow_factory() as uow:
        artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
    version_two = next(
        artifact
        for artifact in artifacts
        if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
        and artifact.version == 2
        and artifact.canonical_blob_id is not None
    )
    version_two_payload = await app.state.production_artifact_store.read_json(
        version_two.canonical_blob_id
    )
    assert (
        editorial_enrichment_from_json(version_two_payload).diagrams[0].compiled_asset_id
        == BASE_DIAGRAM_ASSET
    )

    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    enrichment = editorial_enrichment_from_json(payload)
    assert len(enrichment.diagrams) == 1
    diagram = enrichment.diagrams[0]
    assert diagram.title == "Revised synthetic flow"
    assert diagram.compiled_asset_id is not None
    assert diagram.compiled_asset_id != BASE_DIAGRAM_ASSET
    async with uow_factory() as uow:
        manifest = await uow.media_assets.get(diagram.compiled_asset_id)
    assert manifest is not None and manifest.kind is MediaAssetKind.DIAGRAM_SVG


@pytest.mark.asyncio
async def test_revoked_source_blocks_a_prepared_revision_and_leaves_canonical_untouched(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
    assert current is not None and current.canonical_blob_id is not None
    base_chart = editorial_enrichment_from_json(
        await app.state.production_artifact_store.read_json(current.canonical_blob_id)
    ).charts[0]

    source_id = seeded.extraction.sources[0].source_document_id
    collection = SourceCollection(
        subject_id=seeded.subject.id,
        edition_id=seeded.subject.edition_id,
        requested_url=seeded.extraction.sources[0].canonical_url,
        proposed_role=SourceRole.PRIMARY,
        canonical_url=seeded.extraction.sources[0].canonical_url,
        source_tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        do_not_submit=False,
        state=CollectionState.ARCHIVED,
    )
    async with uow_factory() as uow:
        await uow.source_collections.add_if_absent(collection)
        document = await uow.source_documents.get(source_id)
        assert document is not None
        # Attaching an equivalent collection keeps the effective policy (and
        # its hash) unchanged, so preparation still sees the base fence.
        await uow.source_documents.save(replace(document, source_collection_id=collection.id))
        await uow.commit()

    proposal = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"revise-chart-revoked-{uuid4().hex}",
        patch=ReviseChartPatch(
            element_key=chart_block["element_key"],
            chart=replace(base_chart, title="Revision blocked by a revoked source"),
        ),
    )

    # The source is revoked after preparation but before acceptance: the same
    # evidence is no longer admissible and adoption must refuse under the lock.
    async with uow_factory() as uow:
        stored_collection = await uow.source_collections.get(collection.id)
        assert stored_collection is not None
        await uow.source_collections.save(replace(stored_collection, external_llm_allowed=False))
        await uow.commit()

    with pytest.raises(EditorialProposalAdoptionError) as blocked:
        await _adopt(
            adoption_service, proposal, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"
        )
    assert blocked.value.code in {
        "external_llm_blocked",
        "proposal_base_unavailable",
        "proposal_base_stale",
    }
    assert await _enrichment_versions(uow_factory, seeded.run.id) == [1, 2]
    assert model_gateway.calls == []


@pytest.mark.asyncio
async def test_concurrent_chart_and_diagram_revisions_have_exactly_one_winner(
    uow_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        seeded,
        app,
        model_gateway,
        workspace_service,
        proposal_service,
        adoption_service,
    ) = await _ready_artifact(uow_factory, monkeypatch)
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    chart_block = _enrichment_block(snapshot, kind="chart")
    diagram_block = _enrichment_block(snapshot, kind="diagram")
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
    assert current is not None and current.canonical_blob_id is not None
    base = editorial_enrichment_from_json(
        await app.state.production_artifact_store.read_json(current.canonical_blob_id)
    )
    chart = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(chart_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"concurrent-chart-{uuid4().hex}",
        patch=ReviseChartPatch(
            element_key=chart_block["element_key"],
            chart=replace(base.charts[0], title="Concurrent chart revision"),
        ),
    )
    diagram = await proposal_service.prepare_visual_revision(
        subject_id=seeded.subject.id,
        snapshot_id=UUID(snapshot["snapshot_id"]),
        occurrence_id=UUID(diagram_block["block_id"]),
        actor_id=seeded.actor_id,
        idempotency_key=f"concurrent-diagram-{uuid4().hex}",
        patch=ReviseDiagramPatch(
            element_key=diagram_block["element_key"],
            diagram=replace(base.diagrams[0], title="Concurrent diagram revision"),
        ),
    )
    results = await asyncio.gather(
        _adopt(adoption_service, chart, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"),
        _adopt(adoption_service, diagram, actor_id=seeded.actor_id, key=f"adopt-{uuid4().hex}"),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, EditorialProposalAdoptionResult)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    loss = failures[0]
    assert isinstance(loss, EditorialProposalAdoptionError)
    assert loss.code in {
        "proposal_stale",
        "proposal_base_stale",
        "proposal_state_conflict",
    }
    assert await _enrichment_versions(uow_factory, seeded.run.id) == [1, 2, 3]
    _artifact, payload = await _read_enrichment(app, uow_factory, seeded.run.id)
    enrichment = editorial_enrichment_from_json(payload)
    assert len(enrichment.charts) == 1
    assert len(enrichment.diagrams) == 1
    assert model_gateway.calls == []

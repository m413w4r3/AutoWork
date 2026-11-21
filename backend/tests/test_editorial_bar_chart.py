from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    resolve_occurrence,
    resolve_workspace_target,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.classification import TLP
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalOrigin,
    EditorialProposalState,
)
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialSourceStage,
    EditorialTransition,
    canonical_content_sha256,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    EditorialAnalyticPurposeV1,
    EditorialEnrichmentElementKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure import analytic_chart_renderer


def _reference() -> ExtractionEvidenceRefV1:
    return ExtractionEvidenceRefV1(
        source_document_id=uuid4(),
        kind=EvidenceKind.FACT,
        evidence_key=hashlib.sha256(b"synthetic bar-chart evidence").hexdigest(),
    )


def _bar_chart(
    *,
    values: tuple[str, ...] = ("12", "18"),
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...] | None = None,
) -> ChartSpecV1:
    anchor = uuid4()
    refs = evidence_refs if evidence_refs is not None else tuple(_reference() for _ in values)
    purpose = EditorialAnalyticPurposeV1(
        question="How many synthetic endpoints were documented?",
        available_data="Exact endpoint counts in the admitted evidence.",
        comprehension_gain="Compare the documented counts.",
        scope="Two synthetic source records.",
        evidence_refs=refs,
        knowledge_limits="No extrapolation beyond the cited values.",
        placement_reason="After the selected paragraph.",
    )
    return ChartSpecV1(
        key="synthetic_endpoint_counts",
        kind=ChartKind.BAR,
        title="Documented endpoint counts",
        caption="Synthetic evidence only.",
        placement=EnrichmentPlacementV1(
            EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor,
        ),
        purpose=purpose,
        points=tuple(
            ChartPointV1(
                label=f"Source {index}",
                date=None,
                series="Endpoints",
                value=Decimal(value),
                evidence_refs=(refs[min(index - 1, len(refs) - 1)],),
            )
            for index, value in enumerate(values, start=1)
        ),
        category_axis_label="Source record",
        value_axis_label="Documented count",
        unit="endpoints",
    )


def test_bar_data_requires_finite_evidence_backed_values() -> None:
    chart = _bar_chart()

    with pytest.raises(ValueError, match="finite"):
        _bar_chart(values=("NaN", "18"))

    from cti_app.application.editorial_proposals import _validate_bar_chart_evidence

    evidence_entries = {
        chart.points[0].evidence_refs[0]: {
            "value": "12",
            "evidence_quote": "The synthetic report documents 12 endpoints.",
        },
        chart.points[1].evidence_refs[0]: {
            "value": "18",
            "evidence_quote": "The synthetic report documents 18 endpoints.",
        },
    }
    admitted_refs = frozenset(evidence_entries)
    _validate_bar_chart_evidence(
        chart,
        evidence_entries=evidence_entries,
        admitted_refs=admitted_refs,
    )

    unsupported = _bar_chart(
        values=("12", "19"),
        evidence_refs=tuple(point.evidence_refs[0] for point in chart.points),
    )
    with pytest.raises(ValueError) as caught:
        _validate_bar_chart_evidence(
            unsupported,
            evidence_entries=evidence_entries,
            admitted_refs=admitted_refs,
        )
    assert getattr(caught.value, "code", None) == "bar_chart_insufficient_data"
    assert getattr(caught.value, "validator_code", None) == "bar_chart_value_not_evidenced"

    substring_only_unit = replace(chart, unit="point")
    with pytest.raises(ValueError) as caught:
        _validate_bar_chart_evidence(
            substring_only_unit,
            evidence_entries=evidence_entries,
            admitted_refs=admitted_refs,
        )
    assert getattr(caught.value, "code", None) == "bar_chart_insufficient_data"


def test_bar_point_requires_value_and_unit_in_the_same_admitted_evidence() -> None:
    from cti_app.application.editorial_proposals import _validate_bar_chart_evidence

    value_ref, unit_ref = _reference(), _reference()
    chart = _bar_chart(values=("12", "18"), evidence_refs=(value_ref, unit_ref))
    chart = replace(
        chart,
        points=(
            replace(chart.points[0], evidence_refs=(value_ref, unit_ref)),
            replace(chart.points[1], evidence_refs=(unit_ref,)),
        ),
    )
    evidence_entries = {
        value_ref: {"value": "12", "evidence_quote": "Alpha recorded 12 observations."},
        unit_ref: {
            "value": "Beta recorded 18 endpoints",
            "evidence_quote": "Beta recorded 18 endpoints.",
        },
    }

    with pytest.raises(ValueError) as caught:
        _validate_bar_chart_evidence(
            chart,
            evidence_entries=evidence_entries,
            admitted_refs=frozenset(evidence_entries),
        )

    assert getattr(caught.value, "code", None) == "bar_chart_insufficient_data"
    assert getattr(caught.value, "validator_code", None) == "bar_chart_value_not_evidenced"

    evidence_entries[value_ref] = {
        "value": "Alpha recorded 12 endpoints and Beta recorded 18 endpoints.",
        "evidence_quote": "Alpha recorded 12 endpoints and Beta recorded 18 endpoints.",
    }
    supported = replace(
        chart,
        points=(
            replace(chart.points[0], evidence_refs=(value_ref,)),
            replace(chart.points[1], evidence_refs=(value_ref,)),
        ),
    )
    _validate_bar_chart_evidence(
        supported,
        evidence_entries=evidence_entries,
        admitted_refs=frozenset(evidence_entries),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "https://example.test/report"),
        ("caption", "www.example.test/report"),
        ("category_axis_label", "https://example.test/category"),
        ("value_axis_label", "www.example.test/value"),
        ("unit", "https://example.test/unit"),
        ("point_label", "www.example.test/point"),
        ("series", "https://example.test/series"),
    ],
)
def test_manual_bar_chart_text_rejects_raw_urls(field: str, value: str) -> None:
    from cti_app.application.editorial_proposals import _editorial_bar_chart_text_violation

    chart = _bar_chart()
    if field in {"point_label", "series"}:
        point = chart.points[0]
        point = replace(point, **{"label" if field == "point_label" else field: value})
        chart = replace(chart, points=(point, *chart.points[1:]))
    else:
        chart = replace(chart, **{field: value})

    assert _editorial_bar_chart_text_violation(chart)


def test_manual_validator_code_does_not_expose_user_keys() -> None:
    from cti_app.application.editorial_proposals import (
        EditorialProposalApplicationError,
        _manual_table_validator_code,
    )

    error = EditorialProposalApplicationError(
        "manual_edit_patch_invalid", validator_code="client_case_alpha"
    )

    assert _manual_table_validator_code(error, fallback="manual_edit_patch_invalid") == (
        "manual_edit_patch_invalid"
    )


def test_bar_chart_json_parsing_preserves_exact_decimal_values() -> None:
    from cti_app.application.editorial_proposals import (
        _parse_editorial_patch_payload,
        _validate_bar_chart_evidence,
    )

    payload = _parse_editorial_patch_payload(
        '{"value":0.10000000000000001,"count":18}', exact_chart_numbers=True
    )
    exact_value = payload["value"]
    assert exact_value == Decimal("0.10000000000000001")
    assert exact_value != Decimal("0.1")
    assert payload["count"] == Decimal("18")

    chart = _bar_chart()
    chart = replace(
        chart,
        points=(
            replace(chart.points[0], value=exact_value),
            chart.points[1],
        ),
    )
    evidence_entries = {
        chart.points[0].evidence_refs[0]: {"value": "0.1 endpoints"},
        chart.points[1].evidence_refs[0]: {"value": "18 endpoints"},
    }
    with pytest.raises(ValueError) as caught:
        _validate_bar_chart_evidence(
            chart,
            evidence_entries=evidence_entries,
            admitted_refs=frozenset(evidence_entries),
        )
    assert getattr(caught.value, "validator_code", None) == "bar_chart_value_not_evidenced"


def test_insufficient_data_refuses_without_invented_value() -> None:
    reference = _reference()

    with pytest.raises(ValueError) as caught:
        _bar_chart(values=("12",), evidence_refs=(reference,))

    assert getattr(caught.value, "code", None) == "bar_chart_insufficient_data"
    assert str(caught.value) == (
        "Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves actuelles. "
        "Un tableau descriptif peut être proposé à la place."
    )


def test_bar_svg_compilation_is_deterministic_and_escapes_labels() -> None:
    chart = _bar_chart()
    chart = replace(
        chart,
        points=(
            ChartPointV1(
                label="Source <A>",
                date=None,
                series="Endpoints",
                value=Decimal("12"),
                evidence_refs=chart.points[0].evidence_refs,
            ),
            chart.points[1],
        ),
    )

    first = analytic_chart_renderer.render_bar_svg(chart)
    second = analytic_chart_renderer.render_bar_svg(chart)

    assert first == second
    assert b"Source &lt;A&gt;" in first
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_editorial_proposal_services_build_real_media_store_without_app_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from starlette.requests import Request

    from cti_app.api import editorial_workspace as editorial_workspace_api
    from cti_app.application.blob_storage import BlobStore
    from cti_app.application.blobs import BlobCatalogService
    from cti_app.application.media_assets import MediaAssetStore
    from cti_app.application.persistence import UnitOfWorkFactory
    from cti_app.application.production_artifact_store import ProductionArtifactStore
    from cti_app.config import Settings
    from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer

    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            production_editorial_resource_search_enabled=False,
        ),
    )
    app = FastAPI()
    uow_factory = cast(UnitOfWorkFactory, object())
    blob_store = cast(BlobStore, object())
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    app.state.uow_factory = uow_factory
    app.state.blob_store = blob_store
    app.state.production_artifact_store = artifact_store
    app.state.model_gateway = object()
    app.state.publication_render_service = object()
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/subjects/test/editor/blocks/test/bar-chart-proposals",
            "headers": [],
        }
    )

    manual_service = editorial_workspace_api._manual_proposal_service(request)
    thread_service = editorial_workspace_api._thread_service(request)
    adoption_service = editorial_workspace_api._proposal_adoption_service(request)
    thread_proposal_service = thread_service._proposal_service
    adoption_enrichment_loader = adoption_service._inputs._enrichment_loader

    assert isinstance(manual_service._chart_compiler, AnalyticChartRenderer)
    assert isinstance(manual_service._media_asset_store, MediaAssetStore)
    assert manual_service._media_asset_store._blob_store is artifact_store
    assert manual_service._media_asset_store._uow_factory is uow_factory
    assert thread_proposal_service is not None
    assert isinstance(thread_proposal_service._chart_compiler, AnalyticChartRenderer)
    assert isinstance(thread_proposal_service._media_asset_store, MediaAssetStore)
    assert thread_proposal_service._media_asset_store._blob_store is artifact_store
    assert thread_proposal_service._media_asset_store._uow_factory is uow_factory
    assert isinstance(adoption_enrichment_loader._chart_compiler, AnalyticChartRenderer)
    assert isinstance(adoption_enrichment_loader._media_asset_store, MediaAssetStore)
    assert adoption_enrichment_loader._media_asset_store._blob_store is artifact_store
    assert adoption_enrichment_loader._media_asset_store._uow_factory is uow_factory


class _CanonicalJsonStore:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self._payload = payload

    async def read_json(self, _blob_id: UUID) -> Mapping[str, object]:
        return self._payload


def _paragraph_enrichment_proposal_fixture(
    operation: TargetedEditorialRevisionOperation,
) -> tuple[
    EditorialProposal,
    WorkspaceSnapshotRecord,
    BlockRegistryEntry,
    ProductionArtifact,
    Mapping[str, object],
    Mapping[str, object],
]:

    subject_id, run_id, snapshot_id = uuid4(), uuid4(), uuid4()
    paragraph = {"text": "Synthetic paragraph with documented counts."}
    synthesis = {
        "title": "Synthetic report",
        "lead": [],
        "sections": [{"kind": "overview", "heading": "Counts", "paragraphs": [paragraph]}],
        "timeline": [],
        "uncertainties": [],
        "warnings": [],
    }
    enrichment = {
        "schema_version": "editorial-enrichment-v1",
        "tables": [],
        "charts": [],
        "diagrams": [],
        "source_figures": [],
    }
    synthesis_sha = canonical_content_sha256(synthesis)
    enrichment_sha = canonical_content_sha256(enrichment)
    entry = BlockRegistryEntry(
        block_id=uuid4(),
        kind=EditorialBlockKind.PARAGRAPH,
        parent_block_id=None,
        position=0,
        content_sha256=canonical_content_sha256(paragraph),
        source_stage=EditorialSourceStage.SYNTHESIS,
        structural_pointer="/sections/0/paragraphs/0",
        transition=EditorialTransition.BOOTSTRAP,
    )
    enrichment_artifact = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash="a" * 64,
        canonical_blob_id=uuid4(),
    )
    snapshot = WorkspaceSnapshotRecord(
        snapshot_id=snapshot_id,
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=uuid4(),
        synthesis_version=1,
        synthesis_input_hash="b" * 64,
        synthesis_canonical_sha256=synthesis_sha,
        enrichment_artifact_id=enrichment_artifact.id,
        enrichment_version=enrichment_artifact.version,
        enrichment_input_hash=enrichment_artifact.input_hash,
        enrichment_canonical_sha256=enrichment_sha,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=1,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=PublicationKind.ARTICLE,
        workspace_pin_hash="c" * 64,
        registry_hash="d" * 64,
        block_registry=(entry,),
    )
    proposal = EditorialProposal(
        id=uuid4(),
        subject_id=subject_id,
        production_run_id=run_id,
        origin=EditorialProposalOrigin.MANUAL_PATCH,
        snapshot_id=snapshot_id,
        occurrence_id=entry.block_id,
        base_artifact_id=enrichment_artifact.id,
        base_artifact_version=enrichment_artifact.version,
        base_input_hash=enrichment_artifact.input_hash,
        base_canonical_sha256=enrichment_sha,
        base_routing_hash=None,
        base_pin_hash=snapshot.workspace_pin_hash,
        target_id="synthetic-enrichment-insertion",
        operation=operation.value,
        before_hash=entry.content_sha256,
        patch_blob_ref=uuid4(),
        patch_sha256="e" * 64,
        evidence_refs=(),
        diagnostics={},
        thread_id=None,
        variant_group_id=uuid4(),
        model_run_id=None,
        model_turn_id=None,
        state=EditorialProposalState.PROPOSED,
        state_version=1,
        actor_id=uuid4(),
        idempotency_key="synthetic-manual-proposal",
        request_sha256="f" * 64,
    )
    return proposal, snapshot, entry, enrichment_artifact, enrichment, synthesis


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(TargetedEditorialRevisionOperation.CREATE_TABLE, id="table"),
        pytest.param(TargetedEditorialRevisionOperation.CREATE_BAR_CHART, id="bar-chart"),
        pytest.param(TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE, id="source-figure"),
    ],
)
async def test_paragraph_enrichment_inspection_reads_enrichment_base(
    operation: TargetedEditorialRevisionOperation,
) -> None:
    from cti_app.application.editorial_proposals import EditorialProposalService

    proposal, snapshot, _entry, artifact, enrichment, _synthesis = (
        _paragraph_enrichment_proposal_fixture(operation)
    )
    service = EditorialProposalService(
        uow_factory=cast(UnitOfWorkFactory, object()),
        artifact_store=cast(ProductionArtifactStore, _CanonicalJsonStore(enrichment)),
    )

    assert await service._read_base_text(proposal, artifact, snapshot) is None


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(TargetedEditorialRevisionOperation.CREATE_TABLE, id="table"),
        pytest.param(TargetedEditorialRevisionOperation.CREATE_BAR_CHART, id="bar-chart"),
        pytest.param(TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE, id="source-figure"),
    ],
)
def test_paragraph_enrichment_proposal_pins_follow_synthesis_anchor(
    operation: TargetedEditorialRevisionOperation,
) -> None:
    from cti_app.application.editorial_discussion_context import _supported_proposal_operations
    from cti_app.application.editorial_proposal_adoption import _proposal_stage
    from cti_app.application.editorial_proposals import _proposal_pins_match

    proposal, snapshot, entry, _artifact, _enrichment, synthesis = (
        _paragraph_enrichment_proposal_fixture(operation)
    )
    resolution = resolve_occurrence(snapshot, proposal.occurrence_id)
    resolved_target = resolve_workspace_target(
        entry=entry,
        canonical_content=synthesis,
        operation=operation.value,
    )
    expected_element_kind = (
        EditorialEnrichmentElementKind.TABLE
        if operation is TargetedEditorialRevisionOperation.CREATE_TABLE
        else EditorialEnrichmentElementKind.CHART
        if operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART
        else EditorialEnrichmentElementKind.FIGURE
    )
    paragraph_enrichment_operations = (
        TargetedEditorialRevisionOperation.paragraph_enrichment_insertion_values()
    )

    assert entry.kind.value == "paragraph"
    assert operation.paragraph_enrichment_element_kind is expected_element_kind
    assert paragraph_enrichment_operations == {
        "create_table",
        "create_bar_chart",
        "create_source_figure",
    }
    assert resolved_target.target.element_kind is expected_element_kind
    assert _proposal_pins_match(proposal, resolution)
    assert (
        _proposal_stage(proposal, {"target": resolved_target.target.to_json()}, resolution)
        is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    )
    assert _supported_proposal_operations(
        stage="synthesis",
        block_kind="paragraph",
        offered_operations=set(paragraph_enrichment_operations),
        paragraph_validation_ready=False,
        enrichment_validation_ready=True,
        bar_chart_validation_ready=True,
    ) == ("create_table", "create_bar_chart")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(TargetedEditorialRevisionOperation.CREATE_TABLE, id="table"),
        pytest.param(TargetedEditorialRevisionOperation.CREATE_BAR_CHART, id="bar-chart"),
    ],
)
async def test_adoption_base_context_uses_synthesis_anchor_for_paragraph_enrichment(
    operation: TargetedEditorialRevisionOperation,
) -> None:
    from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionError
    from cti_app.application.editorial_proposal_adoption_inputs import (
        ProductionEditorialProposalAdoptionInputs,
    )

    proposal, record, _entry, enrichment_artifact, enrichment, _synthesis = (
        _paragraph_enrichment_proposal_fixture(operation)
    )
    synthesis_artifact = ProductionArtifact(
        id=record.synthesis_artifact_id,
        production_run_id=record.production_run_id,
        subject_id=record.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=record.synthesis_version,
        input_hash=record.synthesis_input_hash,
        canonical_blob_id=uuid4(),
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=record.production_run_id,
        subject_id=record.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="1" * 64,
        canonical_blob_id=uuid4(),
    )
    run = ProductionRun(
        id=record.production_run_id,
        subject_id=record.subject_id,
        edition_id=uuid4(),
    )
    snapshot = ProductionInputSnapshot(
        production_run_id=run.id,
        edition_id=run.edition_id,
        subject_id=run.subject_id,
        subject_version=1,
        subject_title="Synthetic subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Synthetic discovery summary",
        actor_or_campaign="Synthetic campaign",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 1),
        publication_language="en",
        research_date=date(2026, 1, 1),
    )

    class _ContextRepository:
        def __init__(self, value) -> None:
            self.value = value

        async def get_current_for_subject(self, _subject_id):
            return self.value

        async def get_by_run(self, _run_id):
            return self.value

    class _ArtifactRepository:
        def __init__(self) -> None:
            self.by_id = {
                artifact.id: artifact
                for artifact in (enrichment_artifact, synthesis_artifact, extraction_artifact)
            }
            self.by_stage = {
                artifact.stage.value: artifact
                for artifact in (enrichment_artifact, synthesis_artifact, extraction_artifact)
            }

        async def get(self, artifact_id):
            return self.by_id.get(artifact_id)

        async def get_current(self, _run_id, stage):
            return self.by_stage.get(stage)

    class _Uow:
        def __init__(self) -> None:
            self.production_runs = _ContextRepository(run)
            self.production_input_snapshots = _ContextRepository(snapshot)
            self.production_artifacts = _ArtifactRepository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

    inputs = ProductionEditorialProposalAdoptionInputs(
        uow_factory=_Uow,
        artifact_store=cast(ProductionArtifactStore, object()),
        workspace_service=object(),
        synthesis_loader=object(),
        enrichment_loader=object(),
        enrichment_revision_service=object(),
    )
    resolution = resolve_occurrence(record, proposal.occurrence_id)
    base_bytes = ProductionArtifactStore.canonical_json_bytes(enrichment)

    try:
        result = await inputs._load_base_context(
            proposal,
            base_artifact=enrichment_artifact,
            base_bytes=base_bytes,
            resolution=resolution,
            stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        )
    except EditorialProposalAdoptionError as error:
        pytest.fail(f"valid {operation.value} adoption context was rejected: {error.code}")

    assert result[0] is run
    assert result[1] is snapshot
    assert result[-1] is synthesis_artifact

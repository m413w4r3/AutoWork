import hashlib
import json
from dataclasses import replace
from datetime import date
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.brief_publication_builder import wrap_publication_document_v7
from cti_app.application.diagram_compilation import (
    DIAGRAM_COMPILATION_POLICY_VERSION,
    CompiledDiagram,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    project_blocks,
    registry_hash,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    canonical_editorial_enrichment_hash,
    canonical_synthesis_hash,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.production_repairs import (
    ProductionRepairMaterializationService,
    ProductionRepairProjectionError,
    _PublicationRouteRebuildRequired,
)
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_assembly import (
    PublicationAssemblyResult,
    PublicationAssemblyService,
)
from cti_app.application.publication_builder import (
    PublicationAssemblyValidationError,
    build_article_format_v1,
    build_publication_document_v4,
    build_publication_document_v5,
)
from cti_app.application.publication_qa import qa_publication_v4, qa_publication_v7
from cti_app.application.typst_rendering import TypstRenderer, load_template_bundle
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editorial_figure_keys import ADOPTED_SOURCE_FIGURE_KEY_PREFIX
from cti_app.domain.editorial_workspace import bootstrap_block_registry
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import (
    DiagramEdgeV1,
    DiagramNodeRole,
    DiagramNodeV1,
    DiagramProfile,
    DiagramSpecV1,
    EditorialAnalyticPurposeV1,
    EditorialEnrichmentV1,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    ResolvedSourceFigureV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    SourceFigureLocatorV1,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
    source_figure_id,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionFactV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication import ArtifactType, PublicationAssemblyErrorCode
from cti_app.domain.publication_document import (
    PublicationDocumentV8,
    PublicationDocumentV9,
    parse_publication_document,
    publication_document_v8_to_json,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_publication_builder_v4 import _table as _enrichment_table


def _canonical_inputs() -> tuple[
    ProductionInputSnapshot,
    ProductionReferenceCorpusV1,
    ProductionExtractionV1,
    ProductionSynthesisV1,
]:
    subject_id = UUID(int=1)
    source_document_id = UUID(int=2)
    content_hash = "c" * 64
    snapshot = ProductionInputSnapshot(
        production_run_id=UUID(int=3),
        edition_id=UUID(int=4),
        subject_id=subject_id,
        subject_version=1,
        subject_title="Example subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=UUID(int=5),
        origin_discovery_subject_id=UUID(int=6),
        canonical_discovery_subject_id=UUID(int=7),
        discovery_snapshot_id=UUID(int=8),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Example discovery summary",
        actor_or_campaign="Example actor",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        publication_language="en",
        research_date=date(2025, 2, 1),
    )
    references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=snapshot.research_date,
        production_input_hash=snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url="https://example.com/report",
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                title="Example report",
                publisher="Example publisher",
                published_at=None,
                source_collection_id=UUID(int=9),
                source_document_id=source_document_id,
                discovery_candidate_ids=(),
                collection_state=CollectionState.ARCHIVED,
                content_sha256=content_hash,
                relevance_reason="Relevant report",
                proposed_by_model=False,
                eligible_for_extraction=True,
            ),
        ),
        warnings=(),
    )
    fact = ExtractionFactV1(
        category="actors",
        value="Example actor",
        attack_id=None,
        context="",
        evidence_quote="The source identifies the actor.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_document_id,),
    )
    source = ProductionSourceExtractionV1(
        source_document_id=source_document_id,
        canonical_url="https://example.com/report",
        content_sha256=content_hash,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=ExtractionProfile.FULL,
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(fact,),
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=references_corpus_hash(references),
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(source,),
        omitted_sources=(),
        warnings=(),
    )
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="en",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="[Example actor] Example synthesis",
        lead=(
            SynthesisParagraphV1("Example finding.", (extraction_evidence_refs_v1(extraction)[0],)),
        ),
        sections=(),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    return snapshot, references, extraction, synthesis


class _MemoryBlobCatalog:
    def __init__(self) -> None:
        self.writes: list[tuple[bytes, str, str]] = []
        self.blobs: dict[UUID, bytes] = {}

    async def ingest(self, content, *, logical_bucket: str, mime_type: str):
        body = content.read()
        self.writes.append((body, logical_bucket, mime_type))
        blob_id = UUID(int=49 + len(self.writes))
        self.blobs[blob_id] = body
        return SimpleNamespace(id=blob_id)

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        if blob_id not in self.blobs:
            raise EntityNotFoundError(f"Blob {blob_id} not found")
        return self.blobs[blob_id]


class _RecordingArtifacts:
    """In-memory artifacts; ``terminal_runs`` maps finished run ids to editions."""

    def __init__(self) -> None:
        self.appended: list[ProductionArtifact] = []
        self.terminal_runs: dict[UUID, UUID] = {}

    async def append(self, artifact: ProductionArtifact) -> None:
        self.appended.append(artifact)

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [artifact for artifact in self.appended if artifact.production_run_id == run_id]

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            artifact
            for artifact in await self.list_for_run(run_id)
            if artifact.stage.value == stage
            and artifact.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda artifact: artifact.version, default=None)

    async def find_reusable(
        self, *, edition_id, subject_id, stage, input_hash, not_before=None
    ) -> ProductionArtifact | None:
        matches = [
            artifact
            for artifact in self.appended
            if self.terminal_runs.get(artifact.production_run_id) == edition_id
            and artifact.subject_id == subject_id
            and artifact.stage.value == stage
            and artifact.input_hash == input_hash
            and artifact.status is ProductionArtifactStatus.VERIFIED
            and artifact.canonical_blob_id is not None
        ]
        return matches[-1] if matches else None

    async def mark_stages_stale(self, run_id: UUID, stages: set[str]) -> list[str]:
        for artifact in await self.list_for_run(run_id):
            if artifact.stage.value in stages:
                artifact.status = ProductionArtifactStatus.STALE
        return sorted(stages)


def _service() -> tuple[PublicationAssemblyService, _MemoryBlobCatalog, _RecordingArtifacts]:
    catalog = _MemoryBlobCatalog()
    artifacts = _RecordingArtifacts()
    service = PublicationAssemblyService(ProductionArtifactStore(catalog), artifacts)
    return service, catalog, artifacts


async def _assemble(
    service: PublicationAssemblyService,
    *,
    publication_kind: PublicationKind = PublicationKind.ARTICLE,
    routing_decision_hash: str = "a" * 64,
    routing_policy_version: str = "routing-policy-v1",
    **kwargs: Any,
) -> PublicationAssemblyResult:
    return await service.assemble_publication(
        publication_kind=publication_kind,
        routing_decision_hash=routing_decision_hash,
        routing_policy_version=routing_policy_version,
        **kwargs,
    )


def _run(snapshot: ProductionInputSnapshot) -> ProductionRun:
    return ProductionRun(
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
        id=snapshot.production_run_id,
    )


def _workspace_record_for(
    *,
    run: ProductionRun,
    synthesis: ProductionSynthesisV1,
    enrichment: EditorialEnrichmentV1,
    synthesis_artifact_id: UUID | None = None,
) -> WorkspaceSnapshotRecord:
    synthesis_artifact_id = synthesis_artifact_id or uuid4()
    enrichment_artifact_id = uuid4()
    synthesis_hash = canonical_synthesis_hash(synthesis)
    enrichment_hash = canonical_editorial_enrichment_hash(enrichment)
    entries = (
        *bootstrap_block_registry(
            artifact_id=synthesis_artifact_id,
            canonical_sha256=synthesis_hash,
            source_stage="synthesis",
            canonical_content=production_synthesis_to_json(synthesis),
        ),
        *bootstrap_block_registry(
            artifact_id=enrichment_artifact_id,
            canonical_sha256=enrichment_hash,
            source_stage="editorial_enrichment",
            canonical_content=editorial_enrichment_to_json(enrichment),
        ),
    )
    return WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=run.subject_id,
        production_run_id=run.id,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_version=1,
        synthesis_input_hash=synthesis.production_input_hash,
        synthesis_canonical_sha256=synthesis_hash,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_version=1,
        enrichment_input_hash=enrichment.production_input_hash,
        enrichment_canonical_sha256=enrichment_hash,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=1,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=PublicationKind.ARTICLE,
        workspace_pin_hash="e" * 64,
        registry_hash=registry_hash(entries),
        block_registry=entries,
    )


def _expected_diagnostics(
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    enrichment: EditorialEnrichmentV1,
) -> dict[str, object]:
    return {
        "warnings_by_stage": {
            "references": list(references.warnings),
            "extraction": list(extraction.warnings),
            "synthesis": list(synthesis.warnings),
            "editorial_enrichment": list(enrichment.warnings),
        },
        "upstream": {},
    }


def test_assembly_constructor_has_no_renderer_dependency() -> None:
    parameters = tuple(signature(PublicationAssemblyService).parameters)
    assert parameters[:2] == ("artifact_store", "production_artifacts")
    assert set(parameters) == {
        "artifact_store",
        "production_artifacts",
        "media_asset_store",
        "diagram_compiler",
    }


class _StaleDiagramMediaStore:
    def __init__(self) -> None:
        self.old_asset_id = UUID(int=90)
        self.new_asset_id = UUID(int=91)
        self.stored_sources: list[str] = []

    async def get(self, asset_id: UUID) -> SimpleNamespace:
        assert asset_id == self.old_asset_id
        return SimpleNamespace(policy_version="diagram-d2-svg-v7-elk-compact")

    async def store_diagram(self, compiled: CompiledDiagram, *, source: str) -> SimpleNamespace:
        assert compiled.compiler_policy_version == DIAGRAM_COMPILATION_POLICY_VERSION
        self.stored_sources.append(source)
        return SimpleNamespace(asset_id=self.new_asset_id)


class _CurrentDiagramCompiler:
    def __init__(self) -> None:
        self.calls = 0

    async def compile(self, diagram: DiagramSpecV1) -> CompiledDiagram:
        self.calls += 1
        source_bytes = b"direction: right\n"
        media_bytes = b"<svg></svg>\n"
        return CompiledDiagram(
            diagram_key=diagram.key,
            source_format="d2",
            source_bytes=source_bytes,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            media_type="image/svg+xml",
            media_bytes=media_bytes,
            media_sha256=hashlib.sha256(media_bytes).hexdigest(),
            compiler="d2",
            compiler_version="0.9.0",
            compiler_policy_version=DIAGRAM_COMPILATION_POLICY_VERSION,
        )


@pytest.mark.asyncio
async def test_publication_rebuilds_stale_diagram_svg_without_model_calls() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    run = _run(snapshot)
    catalog = _MemoryBlobCatalog()
    artifacts = _RecordingArtifacts()
    media_asset_store = _StaleDiagramMediaStore()
    diagram_compiler = _CurrentDiagramCompiler()
    service = PublicationAssemblyService(
        ProductionArtifactStore(catalog),
        artifacts,
        media_asset_store=media_asset_store,  # type: ignore[arg-type]
        diagram_compiler=diagram_compiler,
    )
    evidence = extraction_evidence_refs_v1(extraction)[0]
    diagram = DiagramSpecV1(
        key="stale-flow",
        kind=EnrichmentDiagramKind.INFECTION_CHAIN,
        title="Stale diagram",
        caption="Actor delivers the malware",
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
        nodes=(
            DiagramNodeV1("actor", "Actor", (evidence,), DiagramNodeRole.ACTOR),
            DiagramNodeV1("malware", "Malware", (evidence,), DiagramNodeRole.MALWARE_TOOL),
        ),
        edges=(DiagramEdgeV1("actor", "malware", "delivers", (evidence,)),),
        groups=(),
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
        compiled_asset_id=media_asset_store.old_asset_id,
        profile=DiagramProfile.FLOW,
        purpose=EditorialAnalyticPurposeV1(
            question="How does the actor deliver the malware?",
            available_data="The diagram has one actor and one malware node.",
            comprehension_gain="The edge states the documented delivery relation.",
            scope="Only the two diagram nodes and their edge.",
            evidence_refs=(evidence,),
            knowledge_limits="No other activity is represented.",
            placement_reason="Place the flow after its supporting explanation.",
        ),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        diagrams=(diagram,),
    )

    assembly = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    publication = assembly.artifact

    assert publication.canonical_blob_id is not None
    stored_document = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(publication.canonical_blob_id)
    )
    assert diagram_compiler.calls == 1
    assert media_asset_store.stored_sources == [f"production_run:{run.id}:diagram:{diagram.key}"]
    assert stored_document.diagrams[0].asset_id == media_asset_store.new_asset_id
    assert enrichment.diagrams[0].compiled_asset_id == media_asset_store.old_asset_id
    assert assembly.editorial_enrichment == replace(
        enrichment,
        diagrams=(replace(diagram, compiled_asset_id=media_asset_store.new_asset_id),),
    )

    qa_inputs = {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "relevance_projection": None,
        "synthesis": synthesis,
    }
    stale_qa = qa_publication_v7(
        **qa_inputs,
        editorial_enrichment=enrichment,
        publication=stored_document,
    )
    assert stale_qa["passed"] is False
    assert stale_qa["checks"]["exact_projection"] is False
    assert all(value for key, value in stale_qa["checks"].items() if key != "exact_projection")
    assert stale_qa["errors"] == ["Publication differs from canonical Assembly projection"]

    refreshed_qa = qa_publication_v7(
        **qa_inputs,
        editorial_enrichment=assembly.editorial_enrichment,
        publication=stored_document,
    )
    assert refreshed_qa["passed"] is True
    assert all(refreshed_qa["checks"].values())


@pytest.mark.asyncio
async def test_assembly_persists_exact_v8_body_and_one_publication_artifact() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    run = _run(snapshot)
    service, catalog, artifacts = _service()
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    document = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    artifact, _ = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    expected_v7 = wrap_publication_document_v7(
        document,
        base_publication_artifact_id=artifact.id,
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )
    expected_document = PublicationDocumentV8(
        base_publication=expected_v7,
        article_format=build_article_format_v1(document, extraction),
    )
    expected_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(expected_document)
    )
    assert catalog.writes == [
        (expected_bytes, "production-artifacts-canonical", "application/json")
    ]
    assert len(artifacts.appended) == 1
    assert artifacts.appended[0] is artifact
    assert artifact.stage is ProductionArtifactStage.PUBLICATION
    assert artifact.version == 1
    assert artifact.production_run_id == run.id
    assert artifact.subject_id == run.subject_id
    assert artifact.input_hash == service._assembly_input_hash(
        publication_kind=PublicationKind.ARTICLE,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        relevance_projection=None,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )
    assert artifact.canonical_blob_id == UUID(int=50)
    assert artifact.raw_blob_id is None
    assert artifact.rendered_blob_id is None
    assert artifact.metadata == {
        "diagnostics": _expected_diagnostics(references, extraction, synthesis, enrichment)
    }
    document_json = serialize_publication_document(expected_document)
    assert document_json["schema_version"] == "8"
    assert set(document_json) == {
        "schema_version",
        "base_publication",
        "article_format",
    }


@pytest.mark.asyncio
async def test_after_paragraph_assembly_resolves_registered_anchor_and_rebuilds_stably() -> None:
    from cti_app.application.edition_document import (
        EditionDocumentArtifactRef,
        build_edition_document,
    )
    from cti_app.application.publication_augmentation import augment_publication_with_opinion
    from cti_app.domain.classification import TLP
    from cti_app.domain.editions import Edition, EditionStatus
    from tests.test_publication_document_v6 import _approved_opinion

    snapshot, references, extraction, synthesis = _canonical_inputs()
    run = _run(snapshot)
    service, catalog, _artifacts = _service()
    synthesis_artifact_id = uuid4()
    synthesis_hash = canonical_synthesis_hash(synthesis)
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=synthesis_hash,
        source_stage="synthesis",
        canonical_content=production_synthesis_to_json(synthesis),
    )
    anchor = next(item for item in synthesis_entries if item.kind.value == "lead_paragraph")
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor.block_id,
    )
    first = _enrichment_table(evidence_ref, key="z_canonical_first")
    second = _enrichment_table(evidence_ref, key="a_canonical_second")
    assert first.purpose is not None
    assert second.purpose is not None
    tables = (
        replace(
            first,
            placement=placement,
            purpose=replace(first.purpose, question="Which first canonical tool is documented?"),
        ),
        replace(
            second,
            placement=placement,
            purpose=replace(second.purpose, question="Which second canonical tool is documented?"),
        ),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=tables,
    )
    workspace_record = _workspace_record_for(
        run=run,
        synthesis=synthesis,
        enrichment=enrichment,
        synthesis_artifact_id=synthesis_artifact_id,
    )

    assembly = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )
    document = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    rebuilt = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )

    assert isinstance(document, PublicationDocumentV9)
    assert document.registry_hash == workspace_record.registry_hash
    assert [item.element_key for item in document.resolved_placements] == [
        "z_canonical_first",
        "a_canonical_second",
    ]
    assert [item.element_position for item in document.resolved_placements] == [0, 1]
    assert all(item.element_kind == "table" for item in document.resolved_placements)
    assert all(item.anchor_block_id == anchor.block_id for item in document.resolved_placements)
    assert all(item.anchor_pointer == "/lead/0" for item in document.resolved_placements)
    with pytest.raises(ValueError, match="Expected a PublicationDocumentV8"):
        publication_document_v8_to_json(document)
    assert rebuilt.artifact.id == assembly.artifact.id

    placement_qa = qa_publication_v7(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=None,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        publication=document,
    )
    assert placement_qa["passed"] is True, placement_qa

    base_v5 = document.base_publication.base_publication.base_publication
    opinion, citation_context = _approved_opinion(base_v5)
    publication_with_opinion = replace(
        document,
        base_publication=replace(
            document.base_publication,
            base_publication=augment_publication_with_opinion(
                base_v5,
                opinion,
                citation_context,
            ),
        ),
    )
    qa_result = qa_publication_v7(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        relevance_projection=None,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        publication=publication_with_opinion,
    )
    assert qa_result["checks"]["references_then_synthesis"] is True
    assert qa_result["checks"]["lead_is_first_and_not_duplicated"] is True
    assert qa_result["errors"] == [
        "Reader projection contains invalid IOC fields or legacy provenance"
    ]

    edition = Edition(
        id=run.edition_id,
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        state=EditionStatus.OPEN,
    )

    async def get_run(run_id: UUID) -> ProductionRun | None:
        return run if run_id == run.id else None

    async def get_artifact(artifact_id: UUID) -> ProductionArtifact | None:
        return assembly.artifact if artifact_id == assembly.artifact.id else None

    uow = SimpleNamespace(
        production_runs=SimpleNamespace(get=get_run),
        production_artifacts=SimpleNamespace(get=get_artifact),
    )
    composed = await build_edition_document(
        uow,
        ProductionArtifactStore(catalog),
        edition,
        (
            EditionDocumentArtifactRef(
                position=1,
                subject_id=run.subject_id,
                production_run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                artifact_id=assembly.artifact.id,
                artifact_version=assembly.artifact.version,
                input_hash=assembly.artifact.input_hash,
            ),
        ),
        require_current=False,
    )
    composed_document = composed.publications[0].document
    assert isinstance(composed_document, PublicationDocumentV9)
    assert composed_document.resolved_placements == document.resolved_placements
    assert [item.element_key for item in composed_document.resolved_placements] == [
        "z_canonical_first",
        "a_canonical_second",
    ]


@pytest.mark.parametrize("anchor_index", (1, 2), ids=("middle-paragraph", "last-paragraph"))
async def test_snapshot_publication_and_typst_keep_anchored_figures_before_section_end_table(
    tmp_path: Path,
    anchor_index: int,
) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    section_paragraphs = tuple(
        SynthesisParagraphV1(f"Synthetic section paragraph {index + 1}.", (evidence_ref,))
        for index in range(3)
    )
    synthesis = replace(
        synthesis,
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Synthetic technical section",
                section_paragraphs,
            ),
        ),
    )
    run = _run(snapshot)
    synthesis_artifact_id = uuid4()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=canonical_synthesis_hash(synthesis),
        source_stage="synthesis",
        canonical_content=production_synthesis_to_json(synthesis),
    )
    anchor = next(
        entry
        for entry in synthesis_entries
        if entry.structural_pointer == f"/sections/0/paragraphs/{anchor_index}"
    )
    anchor_placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor.block_id,
    )
    section_end_placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_SECTION,
        section_index=0,
    )
    table = replace(
        _enrichment_table(evidence_ref, key="table_at_section_end"),
        placement=section_end_placement,
    )
    source = extraction.sources[0]
    figures: list[SourceFigureCandidateV1] = []
    figure_key_suffixes = ("z_canonical_first", "a_canonical_second")
    for order in range(2):
        locator = SourceFigureLocatorV1(
            page=order + 1,
            section="Synthetic technical section",
            figure_label=f"Synthetic figure {order + 1}",
        )
        sha256 = hashlib.sha256(f"synthetic figure bytes {order}".encode()).hexdigest()
        figure_id = source_figure_id(
            source_document_id=source.source_document_id,
            sha256=sha256,
            source=source.canonical_url,
            locator=locator,
        )
        provenance = "Synthetic archived figure fixture."
        resolved = ResolvedSourceFigureV1(
            figure_id=figure_id,
            blob_id=UUID(int=8100 + order),
            sha256=sha256,
            mime_type="image/png",
            byte_size=128,
            source_document_id=source.source_document_id,
            source=source.canonical_url,
            provenance=provenance,
            locator=locator,
            decision=SourceFigureDecision.ACCEPTED,
            decision_reason="archived_blob_verified",
        )
        figures.append(
            SourceFigureCandidateV1(
                key=f"{ADOPTED_SOURCE_FIGURE_KEY_PREFIX}{figure_key_suffixes[order]}",
                source_document_id=source.source_document_id,
                source_url=source.canonical_url,
                caption=f"Synthetic figure caption {order + 1}",
                provenance=provenance,
                locator=locator,
                inclusion_status=SourceFigureInclusionStatus.INCLUDED,
                placement=anchor_placement,
                resolved_figure=resolved,
                width=8,
                height=6,
                order=order,
            )
        )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(table,),
        source_figures=tuple(figures),
    )
    workspace_record = _workspace_record_for(
        run=run,
        synthesis=synthesis,
        enrichment=enrichment,
        synthesis_artifact_id=synthesis_artifact_id,
    )
    synthesis_payload = production_synthesis_to_json(synthesis)
    enrichment_payload = editorial_enrichment_to_json(enrichment)
    snapshot_blocks, _enrichments, _outline = project_blocks(
        entries=workspace_record.block_registry,
        synthesis=synthesis_payload,
        enrichment=enrichment_payload,
        record=workspace_record,
        allow_operations=False,
    )

    service, catalog, _artifacts = _service()
    assembly = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )
    document = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    assert isinstance(document, PublicationDocumentV9)
    figure_keys = [figure.key for figure in figures]
    assert [placement.element_key for placement in document.resolved_placements] == figure_keys
    assert all(
        placement.anchor_block_id == anchor.block_id for placement in document.resolved_placements
    )
    publication_v5 = document.base_publication.base_publication.base_publication
    assert publication_v5.document.tables[0].placement == section_end_placement

    template_root = tmp_path / "typst-template"
    entrypoint = template_root / "RENDERER" / "publication.typ"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text('#let publication = json("render-data.json")\n', encoding="utf-8")
    (template_root / "renderer-manifest.json").write_text(
        json.dumps({"template_version": "test-v1", "files": ["RENDERER/publication.typ"]}),
        encoding="utf-8",
    )
    rendered = TypstRenderer().render(
        document,
        load_template_bundle(template_root),
        document_label="Synthetic placement fixture",
    )
    typst_payload = json.loads(rendered.render_data_bytes)
    typst_blocks = next(
        section["blocks"]
        for section in typst_payload["content_sections"]
        if section["type"] == "synthesis"
    )

    snapshot_anchor_index = next(
        index
        for index, block in enumerate(snapshot_blocks)
        if block["block_id"] == str(anchor.block_id)
    )
    snapshot_table_index = next(
        index
        for index, block in enumerate(snapshot_blocks)
        if block["kind"] == "table" and block["table"]["key"] == table.key
    )
    typst_anchor_index = next(
        index
        for index, block in enumerate(typst_blocks)
        if block["type"] == "paragraph" and block["text"] == section_paragraphs[anchor_index].text
    )
    typst_table_index = next(
        index
        for index, block in enumerate(typst_blocks)
        if block["type"] == "table" and block["key"] == table.key
    )

    def snapshot_label(block: dict[str, object]) -> tuple[str, str]:
        kind = str(block["kind"])
        if kind in {"paragraph", "lead_paragraph"}:
            return "paragraph", str(block["text"])
        if kind in {"figure", "table"}:
            payload = block[kind]
            assert isinstance(payload, dict)
            return kind, str(payload["key"])
        raise AssertionError(f"Unexpected snapshot block in placement sequence: {kind}")

    def typst_label(block: dict[str, object]) -> tuple[str, str]:
        kind = str(block["type"])
        if kind == "paragraph":
            return kind, str(block["text"])
        if kind in {"figure", "table"}:
            return kind, str(block["key"])
        raise AssertionError(f"Unexpected Typst block in placement sequence: {kind}")

    snapshot_sequence = [
        snapshot_label(block)
        for block in snapshot_blocks[snapshot_anchor_index : snapshot_table_index + 1]
    ]
    typst_sequence = [
        typst_label(block) for block in typst_blocks[typst_anchor_index : typst_table_index + 1]
    ]
    expected_sequence = [
        ("paragraph", section_paragraphs[anchor_index].text),
        *(("figure", key) for key in figure_keys),
        *(("paragraph", paragraph.text) for paragraph in section_paragraphs[anchor_index + 1 :]),
        ("table", table.key),
    ]
    assert snapshot_sequence == expected_sequence
    assert typst_sequence == expected_sequence
    assert snapshot_sequence == typst_sequence


@pytest.mark.asyncio
async def test_assembly_persists_editorial_enrichment_in_the_canonical_body() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence = extraction_evidence_refs_v1(extraction)[0]
    enrichment = _enrichment_citing(extraction, synthesis, evidence)
    service, catalog, _artifacts = _service()
    store = ProductionArtifactStore(catalog)

    artifact, _ = await _assemble(
        service,
        run=_run(snapshot),
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert artifact.rendered_blob_id is None
    assert artifact.canonical_blob_id is not None
    document = parse_publication_document(await store.read_json(artifact.canonical_blob_id))
    assert document.tables[0].key == "observations"
    assert document.tables[0].rows[0].cells == ("Observed", "Reported by the source")


@pytest.mark.asyncio
async def test_brief_assembly_persists_v8_without_editorial_enrichment() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    relevance = build_relevance_projection(snapshot, extraction)
    service, catalog, artifacts = _service()

    result = await _assemble(
        service,
        run=_run(snapshot),
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        relevance_projection=relevance,
        editorial_enrichment=None,
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
    )

    assert result.editorial_enrichment is None
    assert len(artifacts.appended) == 1
    assert artifacts.appended[0].stage is ProductionArtifactStage.PUBLICATION
    assert result.artifact.canonical_blob_id is not None
    publication = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(result.artifact.canonical_blob_id)
    )
    assert isinstance(publication, PublicationDocumentV8)
    assert publication.publication_kind is PublicationKind.BRIEF
    assert publication.article_format is None
    assert publication.indicators == ()
    assert (
        "editorial_enrichment" not in result.artifact.metadata["diagnostics"]["warnings_by_stage"]
    )
    assert (
        qa_publication_v7(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            relevance_projection=relevance,
            synthesis=synthesis,
            editorial_enrichment=None,
            publication=publication,
        )["passed"]
        is True
    )


@pytest.mark.asyncio
async def test_brief_assembly_projects_valid_enrichment_and_records_its_identity() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence = extraction_evidence_refs_v1(extraction)[0]
    enrichment = _enrichment_citing(extraction, synthesis, evidence)
    relevance = build_relevance_projection(snapshot, extraction)
    enrichment_artifact_id = UUID(int=9901)
    enrichment_artifact_input_hash = "e" * 64
    service, catalog, _artifacts = _service()

    result = await _assemble(
        service,
        run=_run(snapshot),
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        relevance_projection=relevance,
        publication_kind=PublicationKind.BRIEF,
        editorial_enrichment_artifact_id=enrichment_artifact_id,
        editorial_enrichment_artifact_input_hash=enrichment_artifact_input_hash,
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
        metadata_extra={
            "input_artifacts": {
                "editorial_enrichment_artifact_id": str(enrichment_artifact_id),
            },
            "diagnostics": {"editorial_enrichment": {"inventory": "archived"}},
        },
    )

    assert result.editorial_enrichment is enrichment
    assert result.artifact.canonical_blob_id is not None
    publication = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(result.artifact.canonical_blob_id)
    )
    assert publication.tables[0].key == "observations"
    assert (
        qa_publication_v7(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            relevance_projection=relevance,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
            publication=publication,
        )["passed"]
        is True
    )
    consumed_identity = PublicationAssemblyService._assembly_input_hash(
        publication_kind=PublicationKind.BRIEF,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=enrichment_artifact_id,
        editorial_enrichment_artifact_input_hash=enrichment_artifact_input_hash,
        relevance_projection=relevance,
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
    )
    changed_input_identity = PublicationAssemblyService._assembly_input_hash(
        publication_kind=PublicationKind.BRIEF,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=enrichment_artifact_id,
        editorial_enrichment_artifact_input_hash="f" * 64,
        relevance_projection=relevance,
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
    )
    ignored_identity = PublicationAssemblyService._assembly_input_hash(
        publication_kind=PublicationKind.BRIEF,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=None,
        editorial_enrichment_artifact_id=enrichment_artifact_id,
        editorial_enrichment_artifact_input_hash=enrichment_artifact_input_hash,
        relevance_projection=relevance,
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
    )
    identity_without_consumption = PublicationAssemblyService._assembly_input_hash(
        publication_kind=PublicationKind.BRIEF,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=None,
        relevance_projection=relevance,
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
    )
    changed_artifact_identity = PublicationAssemblyService._assembly_input_hash(
        publication_kind=PublicationKind.BRIEF,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=UUID(int=9902),
        editorial_enrichment_artifact_input_hash=enrichment_artifact_input_hash,
        relevance_projection=relevance,
        routing_decision_hash="b" * 64,
        routing_policy_version="routing-policy-v1",
    )

    assert result.artifact.input_hash == consumed_identity
    assert consumed_identity != changed_input_identity
    assert consumed_identity != changed_artifact_identity
    assert ignored_identity == identity_without_consumption


@pytest.mark.asyncio
async def test_empty_brief_enrichment_is_not_consumed_or_added_to_assembly_identity() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    relevance = build_relevance_projection(snapshot, extraction)
    empty_enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    arguments = {
        "run": _run(snapshot),
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "relevance_projection": relevance,
        "publication_kind": PublicationKind.BRIEF,
        "routing_decision_hash": "b" * 64,
        "routing_policy_version": "routing-policy-v1",
    }
    absent_service, _absent_catalog, _ = _service()
    empty_service, _empty_catalog, _ = _service()

    absent = await _assemble(absent_service, editorial_enrichment=None, **arguments)
    empty = await _assemble(
        empty_service,
        editorial_enrichment=empty_enrichment,
        editorial_enrichment_artifact_id=UUID(int=9903),
        editorial_enrichment_artifact_input_hash="a" * 64,
        metadata_extra={
            "input_artifacts": {"editorial_enrichment_artifact_id": str(UUID(int=9903))},
            "diagnostics": {"editorial_enrichment": {"pending": 1}},
        },
        **arguments,
    )

    assert empty.editorial_enrichment is None
    assert empty.artifact.input_hash == absent.artifact.input_hash
    assert "editorial_enrichment_artifact_id" not in empty.artifact.metadata["input_artifacts"]


@pytest.mark.asyncio
async def test_identical_inputs_in_the_same_run_return_the_current_publication() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    service, catalog, artifacts = _service()
    inputs = {
        "run": _run(snapshot),
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": build_empty_editorial_enrichment(
            extraction=extraction, synthesis=synthesis
        ),
    }

    first, _ = await _assemble(service, **inputs)
    second, _ = await _assemble(service, **inputs)

    assert second is first
    assert len(catalog.writes) == 1
    assert artifacts.appended == [first]


@pytest.mark.asyncio
async def test_identical_inputs_reuse_a_finished_run_publication_with_provenance() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    service, catalog, artifacts = _service()
    source_run = _run(snapshot)
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    source, _ = await _assemble(
        service,
        run=source_run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    artifacts.terminal_runs[source_run.id] = source_run.edition_id
    target_snapshot = replace(snapshot, production_run_id=UUID(int=30))
    target_run = _run(target_snapshot)

    reused, _ = await _assemble(
        service,
        run=target_run,
        snapshot=target_snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    assert len(catalog.writes) == 1
    assert artifacts.appended == [source, reused]
    assert reused.production_run_id == target_run.id
    assert reused.stage is ProductionArtifactStage.PUBLICATION
    assert reused.version == 1
    assert reused.input_hash == source.input_hash
    assert reused.canonical_blob_id == source.canonical_blob_id
    assert reused.rendered_blob_id is None
    assert reused.reused_from_artifact_id == source.id
    assert reused.metadata == {
        "diagnostics": _expected_diagnostics(references, extraction, synthesis, enrichment),
        "reused": True,
        "reused_from_artifact_id": str(source.id),
        "reused_from_created_at": source.created_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_publication_warnings_are_artifact_diagnostics_not_document_content() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    references = replace(references, warnings=("reference_invalid_url",))
    extraction = replace(
        extraction,
        references_corpus_hash=references_corpus_hash(references),
        warnings=("extraction_source_skipped:https://example.test:unavailable",),
    )
    synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
        warnings=("synthesis_output_invalid",),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        warnings=("editorial_enrichment_invalid",),
    )
    service, catalog, _artifacts = _service()
    store = ProductionArtifactStore(catalog)

    artifact, _ = await _assemble(
        service,
        run=_run(snapshot),
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    document_json = await store.read_json(artifact.canonical_blob_id)

    assert artifact.metadata["diagnostics"] == _expected_diagnostics(
        references, extraction, synthesis, enrichment
    )
    assert "extraction_source_skipped" not in str(document_json)
    assert "synthesis_output_invalid" not in str(document_json)


@pytest.mark.asyncio
async def test_unreadable_reuse_candidate_falls_back_to_fresh_persistence() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    service, catalog, artifacts = _service()
    source_run = _run(snapshot)
    source, _ = await _assemble(
        service,
        run=source_run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=build_empty_editorial_enrichment(
            extraction=extraction, synthesis=synthesis
        ),
    )
    artifacts.terminal_runs[source_run.id] = source_run.edition_id
    catalog.blobs.clear()
    target_snapshot = replace(snapshot, production_run_id=UUID(int=30))

    fresh, _ = await _assemble(
        service,
        run=_run(target_snapshot),
        snapshot=target_snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=build_empty_editorial_enrichment(
            extraction=extraction, synthesis=synthesis
        ),
    )

    assert len(catalog.writes) == 2
    assert fresh.reused_from_artifact_id is None
    assert fresh.canonical_blob_id != source.canonical_blob_id


@pytest.mark.asyncio
async def test_changed_inputs_persist_a_fresh_body_as_the_next_revision() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    service, catalog, artifacts = _service()
    run = _run(snapshot)
    artifacts.terminal_runs[run.id] = run.edition_id
    first, _ = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=build_empty_editorial_enrichment(
            extraction=extraction, synthesis=synthesis
        ),
    )
    changed_synthesis = replace(synthesis, title="[Example actor] Revised synthesis")

    second, _ = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=changed_synthesis,
        editorial_enrichment=build_empty_editorial_enrichment(
            extraction=extraction, synthesis=changed_synthesis
        ),
    )

    changed_enrichment = build_empty_editorial_enrichment(
        extraction=extraction, synthesis=changed_synthesis
    )
    expected = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=changed_synthesis,
        editorial_enrichment=changed_enrichment,
    )
    expected_v7 = wrap_publication_document_v7(
        expected,
        base_publication_artifact_id=second.id,
        publication_kind=PublicationKind.ARTICLE,
        routing_decision_hash="a" * 64,
        routing_policy_version="routing-policy-v1",
    )
    expected_v8 = PublicationDocumentV8(
        base_publication=expected_v7,
        article_format=build_article_format_v1(expected, extraction),
    )
    assert (first.version, second.version) == (1, 2)
    assert second.input_hash != first.input_hash
    assert second.reused_from_artifact_id is None
    assert second.canonical_blob_id != first.canonical_blob_id
    assert len(catalog.writes) == 2
    assert catalog.writes[1][0] == ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(expected_v8)
    )
    assert artifacts.appended == [first, second]


def test_canonical_qa_rejects_added_claim_and_legacy_marker() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    inputs = {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": enrichment,
    }

    assert qa_publication_v4(publication=document, **inputs)["passed"] is True
    injected = replace(document, title="Added editorial claim")
    result = qa_publication_v4(publication=injected, **inputs)
    assert result["checks"]["exact_projection"] is False
    assert result["passed"] is False

    legacy = replace(document, title=f"{document.title} [S1]")
    result = qa_publication_v4(publication=legacy, **inputs)
    assert result["checks"]["no_legacy_citation"] is False
    assert result["passed"] is False


def test_confirmed_ioc_with_invalid_normalization_blocks_assembly() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    invalid = ExtractionIndicatorV1(
        value="not an IP address",
        artifact_type=ArtifactType.IP,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="",
        evidence_quote="Source text",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source.source_document_id,),
    )
    extraction = replace(extraction, sources=(replace(source, indicators=(invalid,)),))
    synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))
    with pytest.raises(ValueError, match="cannot be normalized"):
        build_publication_document_v4(
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=build_empty_editorial_enrichment(
                extraction=extraction, synthesis=synthesis
            ),
        )


def _enrichment_citing(
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    ref: ExtractionEvidenceRefV1,
) -> EditorialEnrichmentV1:
    return replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(
            TableSpecV1(
                key="observations",
                kind=EnrichmentTableKind.CUSTOM,
                title="Observations",
                caption=None,
                columns=(TableColumnV1("item", "Item"), TableColumnV1("note", "Note")),
                rows=(TableRowV1(("Observed", "Reported by the source"), (ref,)),),
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.END),
                purpose=EditorialAnalyticPurposeV1(
                    question="What observation is reported?",
                    available_data="One observation and its source context.",
                    comprehension_gain="The row keeps the observation beside its context.",
                    scope="The cited source observation.",
                    evidence_refs=(ref,),
                    knowledge_limits="No further details are reported.",
                    placement_reason="Place after the synthesis passage about the observation.",
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_publication_v5_review_projection_retains_figure_provenance_and_origin() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    locator = SourceFigureLocatorV1(
        page=2,
        section="Network overview",
        figure_label="BlueMoon exploitation chain",
        original_asset_url="https://cdn.example.com/bluemoon-chain.png",
    )
    figure_id = source_figure_id(
        source_document_id=source.source_document_id,
        sha256="f" * 64,
        source=source.canonical_url,
        locator=locator,
    )
    resolved = ResolvedSourceFigureV1(
        figure_id=figure_id,
        blob_id=UUID(int=21),
        sha256="f" * 64,
        mime_type="image/png",
        byte_size=4096,
        source_document_id=source.source_document_id,
        source=source.canonical_url,
        provenance="Archived figure from the primary source, page 2",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="accepted_for_review",
    )
    candidate = SourceFigureCandidateV1(
        key=f"source_figure_{figure_id.hex}",
        source_document_id=source.source_document_id,
        source_url=source.canonical_url,
        caption="BlueMoon exploitation chain from lure to payloads",
        provenance=resolved.provenance,
        locator=locator,
        inclusion_status=SourceFigureInclusionStatus.INCLUDED,
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
        resolved_figure=resolved,
    )
    enrichment = replace(
        _enrichment_citing(extraction, synthesis, extraction_evidence_refs_v1(extraction)[0]),
        source_figures=(candidate,),
    )

    document = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    payload = serialize_publication_document(document)
    published_figure = payload["figures"][0]

    assert published_figure["source_url"] == source.canonical_url
    assert published_figure["locator"]["original_asset_url"] == (
        "https://cdn.example.com/bluemoon-chain.png"
    )
    assert published_figure["sha256"] == "f" * 64
    assert published_figure["provenance"] == "Archived figure from the primary source, page 2"

    service, _catalog, _artifacts = _service()
    artifact, _ = await _assemble(
        service,
        run=_run(snapshot),
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    (diagnostic,) = artifact.metadata["source_figure_provenance_diagnostics"]
    assert diagnostic["stage"] == "published"
    assert diagnostic["figure_id"] == str(figure_id)
    assert diagnostic["blob_id"] == str(UUID(int=21))
    assert diagnostic["sha256"] == published_figure["sha256"]
    assert set(diagnostic) == {
        "stage",
        "source_document_id",
        "figure_id",
        "figure_handle",
        "blob_id",
        "sha256",
        "reason_code",
    }


@pytest.mark.asyncio
async def test_ioc_only_repair_reuses_narrative_and_reassembles_canonical_publication() -> None:
    snapshot, references, extraction_a, synthesis_a = _canonical_inputs()
    source = extraction_a.sources[0]
    indicator = ExtractionIndicatorV1(
        value="ioc.example",
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="Example actor used the malicious domain for command and control.",
        evidence_quote=("Example actor used the malicious domain for command and control."),
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source.source_document_id,),
    )
    extraction_b = replace(
        extraction_a,
        sources=(replace(source, indicators=(indicator,)),),
    )
    service, catalog, artifacts = _service()
    store = ProductionArtifactStore(catalog)
    run = _run(snapshot)

    async def artifact(
        stage: ProductionArtifactStage, version: int, payload: dict[str, object]
    ) -> ProductionArtifact:
        _, blob_id, _ = await store.store_stage_payloads(canonical=payload)
        assert blob_id is not None
        row = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=version,
            input_hash=f"{version}" * 64,
            canonical_blob_id=blob_id,
        )
        await artifacts.append(row)
        return row

    await artifact(
        ProductionArtifactStage.REFERENCES, 1, production_reference_corpus_to_json(references)
    )
    await artifact(
        ProductionArtifactStage.EXTRACTION, 1, production_extraction_to_json(extraction_a)
    )
    await artifact(ProductionArtifactStage.SYNTHESIS, 1, production_synthesis_to_json(synthesis_a))
    fact_ref = next(
        ref for ref in extraction_evidence_refs_v1(extraction_a) if ref.kind is EvidenceKind.FACT
    )
    enrichment_a = _enrichment_citing(extraction_a, synthesis_a, fact_ref)
    enrichment_a_artifact = await artifact(
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        1,
        editorial_enrichment_to_json(enrichment_a),
    )
    publication_a, _ = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction_a,
        synthesis=synthesis_a,
        editorial_enrichment=enrichment_a,
    )
    extraction_b_artifact = await artifact(
        ProductionArtifactStage.EXTRACTION, 2, production_extraction_to_json(extraction_b)
    )

    class Snapshots:
        async def get_by_run(self, run_id: UUID) -> ProductionInputSnapshot | None:
            return snapshot if run_id == run.id else None

    class RoutingDecisions:
        def __init__(self) -> None:
            self.decisions = {}

        async def get(self, decision_id: UUID):
            return self.decisions.get(decision_id)

        async def append_and_adopt(self, decision, *, expected_version: int):
            assert expected_version == run.version
            self.decisions[decision.decision_id] = decision
            return decision

    uow = SimpleNamespace(
        production_artifacts=artifacts,
        production_input_snapshots=Snapshots(),
        production_routing_decisions=RoutingDecisions(),
    )
    repair = ProductionRepairMaterializationService(
        lambda: None,  # type: ignore[arg-type]
        projection_service=object(),  # type: ignore[arg-type]
        artifact_store=store,
    )
    publication_b, qa = await repair._materialize_canonical_publication_in_uow(
        uow,
        run=run,
        extraction=extraction_b_artifact,
        repair_materialization=None,
    )
    assert qa["passed"] is True
    assert publication_b.id != publication_a.id
    assert publication_b.input_hash != publication_a.input_hash
    assert publication_b.version == 2
    assert publication_b.canonical_blob_id is not None
    document = parse_publication_document(await store.read_json(publication_b.canonical_blob_id))
    assert document.lead[0].text == synthesis_a.lead[0].text
    assert [
        (group.artifact_type, item.normalized_value)
        for group in document.indicators
        for item in group.indicators
    ] == [(ArtifactType.DOMAIN, "ioc.example")]
    syntheses = [
        row for row in artifacts.appended if row.stage is ProductionArtifactStage.SYNTHESIS
    ]
    assert len(syntheses) == 2
    assert syntheses[-1].reused_from_artifact_id == syntheses[0].id
    assert syntheses[-1].model_run_id is None
    # The model-generated enrichment follows the repair verbatim: no model
    # call, no artificial empty enrichment.
    enrichments = [
        row
        for row in artifacts.appended
        if row.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ]
    assert len(enrichments) == 2
    assert enrichments[-1].reused_from_artifact_id == enrichment_a_artifact.id
    assert enrichments[-1].metadata["lineage_rebased"] is True
    assert enrichments[-1].canonical_blob_id is not None
    rebased = editorial_enrichment_from_json(
        await store.read_json(enrichments[-1].canonical_blob_id)
    )
    assert rebased.tables == enrichment_a.tables
    assert rebased.extraction_hash == canonical_extraction_hash(extraction_b)

    extraction_c = replace(
        extraction_b,
        sources=(replace(extraction_b.sources[0], facts=()),),
    )
    extraction_c_artifact = await artifact(
        ProductionArtifactStage.EXTRACTION, 3, production_extraction_to_json(extraction_c)
    )
    with pytest.raises(ProductionRepairProjectionError, match="assembly_evidence_missing"):
        await repair._materialize_canonical_publication_in_uow(
            uow,
            run=run,
            extraction=extraction_c_artifact,
            repair_materialization=None,
        )
    assert (
        len([row for row in artifacts.appended if row.stage is ProductionArtifactStage.PUBLICATION])
        == 2
    )


@pytest.mark.asyncio
async def test_ioc_repair_route_change_requires_synthesis_rebuild() -> None:
    snapshot, references, extraction_a, synthesis_a = _canonical_inputs()
    source = extraction_a.sources[0]
    indicator = ExtractionIndicatorV1(
        value="ioc.example",
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="",
        evidence_quote="The source identifies the domain.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source.source_document_id,),
    )
    extraction_b = replace(extraction_a, sources=(replace(source, indicators=(indicator,)),))
    synthesis_b = replace(synthesis_a, extraction_hash=canonical_extraction_hash(extraction_b))
    indicator_ref = next(
        ref
        for ref in extraction_evidence_refs_v1(extraction_b)
        if ref.kind is EvidenceKind.INDICATOR
    )
    _service_unused, catalog, artifacts = _service()
    store = ProductionArtifactStore(catalog)
    run = _run(snapshot)
    previous_route_id = UUID(int=20)
    run.routing_decision_id = previous_route_id

    async def artifact(
        stage: ProductionArtifactStage, version: int, payload: dict[str, object]
    ) -> ProductionArtifact:
        _, blob_id, _ = await store.store_stage_payloads(canonical=payload)
        assert blob_id is not None
        row = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=version,
            input_hash=f"{version}" * 64,
            canonical_blob_id=blob_id,
        )
        await artifacts.append(row)
        return row

    await artifact(
        ProductionArtifactStage.REFERENCES, 1, production_reference_corpus_to_json(references)
    )
    await artifact(
        ProductionArtifactStage.EXTRACTION, 1, production_extraction_to_json(extraction_b)
    )
    await artifact(ProductionArtifactStage.SYNTHESIS, 1, production_synthesis_to_json(synthesis_b))
    await artifact(
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        1,
        editorial_enrichment_to_json(_enrichment_citing(extraction_b, synthesis_b, indicator_ref)),
    )
    # The repair excludes the only indicator the enrichment table cites.
    extraction_c_artifact = await artifact(
        ProductionArtifactStage.EXTRACTION, 2, production_extraction_to_json(extraction_a)
    )

    class Snapshots:
        async def get_by_run(self, run_id: UUID) -> ProductionInputSnapshot | None:
            return snapshot if run_id == run.id else None

    class RoutingDecisions:
        def __init__(self) -> None:
            self.decisions = {
                previous_route_id: SimpleNamespace(
                    publication_kind=PublicationKind.ARTICLE,
                    policy_version="previous-routing-policy",
                )
            }

        async def get(self, decision_id: UUID):
            return self.decisions.get(decision_id)

        async def append_and_adopt(self, decision, *, expected_version: int):
            assert expected_version == run.version
            self.decisions[decision.decision_id] = decision
            return decision

    uow = SimpleNamespace(
        production_artifacts=artifacts,
        production_input_snapshots=Snapshots(),
        production_routing_decisions=RoutingDecisions(),
    )
    repair = ProductionRepairMaterializationService(
        lambda: None,  # type: ignore[arg-type]
        projection_service=object(),  # type: ignore[arg-type]
        artifact_store=store,
    )
    with pytest.raises(_PublicationRouteRebuildRequired) as raised:
        await repair._materialize_canonical_publication_in_uow(
            uow,
            run=run,
            extraction=extraction_c_artifact,
            repair_materialization=None,
        )
    assert raised.value.previous_kind is PublicationKind.ARTICLE
    assert raised.value.publication_kind is PublicationKind.BRIEF
    assert [
        row.version
        for row in artifacts.appended
        if row.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT
    ] == [1]
    assert not [
        row for row in artifacts.appended if row.stage is ProductionArtifactStage.PUBLICATION
    ]


@pytest.mark.parametrize(
    "invalid_part", ("run", "edition", "subject", "hash", "evidence", "source")
)
@pytest.mark.asyncio
async def test_invalid_lineage_fails_before_body_or_artifact_persistence(
    invalid_part: str,
) -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    run = _run(snapshot)
    if invalid_part == "run":
        run = replace(run, id=UUID(int=99))
    elif invalid_part == "edition":
        run = replace(run, edition_id=UUID(int=99))
    elif invalid_part == "subject":
        references = replace(references, subject_id=UUID(int=99))
    elif invalid_part == "hash":
        extraction = replace(extraction, production_input_hash="0" * 64)
    elif invalid_part == "evidence":
        invalid_ref = replace(extraction_evidence_refs_v1(extraction)[0], evidence_key="0" * 64)
        synthesis = replace(
            synthesis,
            lead=(SynthesisParagraphV1("Invalid citation.", (invalid_ref,)),),
        )
    else:
        references = replace(
            references,
            sources=(replace(references.sources[0], source_document_id=UUID(int=99)),),
        )
        extraction = replace(extraction, references_corpus_hash=references_corpus_hash(references))
        synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))

    service, catalog, artifacts = _service()
    with pytest.raises(PublicationAssemblyValidationError) as failure:
        await _assemble(
            service,
            run=run,
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=build_empty_editorial_enrichment(
                extraction=extraction, synthesis=synthesis
            ),
        )

    assert failure.value.code is (
        PublicationAssemblyErrorCode.EVIDENCE_MISSING
        if invalid_part == "evidence"
        else PublicationAssemblyErrorCode.SOURCE_MISSING
        if invalid_part == "source"
        else PublicationAssemblyErrorCode.INPUTS_MISMATCH
    )

    assert catalog.writes == []
    assert artifacts.appended == []


def _anchored_source_figure(
    extraction: Any,
    *,
    key: str,
    placement: EnrichmentPlacementV1,
    order: int,
    ordinal: int,
):
    locator = SourceFigureLocatorV1(
        page=ordinal + 1,
        section="Synthetic anchored section",
        figure_label=f"Synthetic anchored figure {ordinal + 1}",
    )
    sha256 = hashlib.sha256(f"synthetic anchored figure {ordinal}".encode()).hexdigest()
    source = extraction.sources[0]
    figure_id = source_figure_id(
        source_document_id=source.source_document_id,
        sha256=sha256,
        source=source.canonical_url,
        locator=locator,
    )
    return SourceFigureCandidateV1(
        key=key,
        source_document_id=source.source_document_id,
        source_url=source.canonical_url,
        caption=f"Synthetic anchored caption {ordinal + 1}",
        provenance="Synthetic archived figure fixture.",
        locator=locator,
        inclusion_status=SourceFigureInclusionStatus.INCLUDED,
        placement=placement,
        resolved_figure=ResolvedSourceFigureV1(
            figure_id=figure_id,
            blob_id=UUID(int=8200 + ordinal),
            sha256=sha256,
            mime_type="image/png",
            byte_size=96,
            source_document_id=source.source_document_id,
            source=source.canonical_url,
            provenance="Synthetic archived figure fixture.",
            locator=locator,
            decision=SourceFigureDecision.ACCEPTED,
            decision_reason="archived_blob_verified",
        ),
        width=8,
        height=6,
        order=order,
    )


def _anchored_diagram(
    ref: ExtractionEvidenceRefV1,
    *,
    key: str,
    placement: EnrichmentPlacementV1,
    group_order: int,
):
    return DiagramSpecV1(
        key=key,
        kind=EnrichmentDiagramKind.INFECTION_CHAIN,
        profile=DiagramProfile.FLOW,
        title="Synthetic ordered diagram",
        caption="Synthetic diagram caption.",
        direction=EnrichmentDiagramDirection.LEFT_TO_RIGHT,
        nodes=(
            DiagramNodeV1("loader", "Loader", (ref,)),
            DiagramNodeV1("payload", "Payload", (ref,), role=DiagramNodeRole.MALWARE_TOOL),
        ),
        edges=(DiagramEdgeV1("loader", "payload", "loads", (ref,)),),
        groups=(),
        placement=placement,
        compiled_asset_id=UUID(int=8300),
        purpose=EditorialAnalyticPurposeV1(
            question="Which ordered diagram is documented?",
            available_data="The source records one loader action.",
            comprehension_gain="The edge exposes the documented action.",
            scope="The two synthetic nodes.",
            evidence_refs=(ref,),
            knowledge_limits="No further stages are documented.",
            placement_reason="Placed after the anchor paragraph.",
        ),
        group_order=group_order,
    )


@pytest.mark.asyncio
async def test_assembly_legacy_order_ignores_figure_order_for_resolved_positions() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    run = _run(snapshot)
    synthesis_artifact_id = uuid4()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=canonical_synthesis_hash(synthesis),
        source_stage="synthesis",
        canonical_content=production_synthesis_to_json(synthesis),
    )
    anchor = next(entry for entry in synthesis_entries if entry.kind.value == "lead_paragraph")
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor.block_id,
    )
    figures = (
        _anchored_source_figure(
            extraction, key="figure_family_first", placement=placement, order=1, ordinal=0
        ),
        _anchored_source_figure(
            extraction, key="figure_family_second", placement=placement, order=0, ordinal=1
        ),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        source_figures=figures,
    )
    workspace_record = _workspace_record_for(
        run=run,
        synthesis=synthesis,
        enrichment=enrichment,
        synthesis_artifact_id=synthesis_artifact_id,
    )
    service, catalog, _artifacts = _service()
    assembly = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )
    document = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    assert isinstance(document, PublicationDocumentV9)
    assert [item.element_key for item in document.resolved_placements] == [
        "figure_family_first",
        "figure_family_second",
    ]
    assert [item.element_position for item in document.resolved_placements] == [0, 1]


@pytest.mark.asyncio
async def test_assembly_explicit_group_order_drives_resolved_positions_and_rebuilds() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    run = _run(snapshot)
    synthesis_artifact_id = uuid4()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=canonical_synthesis_hash(synthesis),
        source_stage="synthesis",
        canonical_content=production_synthesis_to_json(synthesis),
    )
    anchor = next(entry for entry in synthesis_entries if entry.kind.value == "lead_paragraph")
    placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor.block_id,
    )
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    first = _enrichment_table(evidence_ref, key="table_ordered_second")
    second = _enrichment_table(evidence_ref, key="table_ordered_first")
    assert first.purpose is not None
    assert second.purpose is not None
    tables = (
        replace(
            first,
            placement=placement,
            group_order=2,
            purpose=replace(first.purpose, question="Which second ordered table is documented?"),
        ),
        replace(
            second,
            placement=placement,
            group_order=0,
            purpose=replace(second.purpose, question="Which first ordered table is documented?"),
        ),
    )
    diagram = _anchored_diagram(
        evidence_ref,
        key="diagram_ordered_middle",
        placement=placement,
        group_order=1,
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        schema_version=11,
        enrichment_policy_version="editorial-enrichment-v15-element-order",
        tables=tables,
        diagrams=(diagram,),
    )
    workspace_record = _workspace_record_for(
        run=run,
        synthesis=synthesis,
        enrichment=enrichment,
        synthesis_artifact_id=synthesis_artifact_id,
    )
    service, catalog, _artifacts = _service()
    assembly = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )
    document = parse_publication_document(
        await ProductionArtifactStore(catalog).read_json(assembly.artifact.canonical_blob_id)
    )
    assert isinstance(document, PublicationDocumentV9)
    assert [
        (item.element_kind, item.element_key, item.element_position)
        for item in document.resolved_placements
    ] == [
        ("table", "table_ordered_first", 0),
        ("diagram", "diagram_ordered_middle", 1),
        ("table", "table_ordered_second", 2),
    ]

    rebuilt = await _assemble(
        service,
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        editorial_enrichment_artifact_id=workspace_record.enrichment_artifact_id,
        workspace_snapshot_record=workspace_record,
    )
    assert rebuilt.artifact.id == assembly.artifact.id

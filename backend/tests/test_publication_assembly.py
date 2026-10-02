from dataclasses import replace
from datetime import date
from inspect import signature
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_repairs import (
    ProductionRepairMaterializationService,
    ProductionRepairProjectionError,
    _EditorialEnrichmentRebuildRequired,
)
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_assembly import PublicationAssemblyService
from cti_app.application.publication_builder import (
    PublicationAssemblyValidationError,
    build_publication_document_v4,
    compute_assembly_input_hash,
)
from cti_app.application.publication_qa import qa_publication_v4
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
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
    EditorialEnrichmentV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
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
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication import ArtifactType, PublicationAssemblyErrorCode
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    parse_publication_document,
    serialize_publication_document,
)
from tests.editorial_enrichment_support import build_empty_editorial_enrichment


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
        title="Example synthesis",
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


def _run(snapshot: ProductionInputSnapshot) -> ProductionRun:
    return ProductionRun(
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
        id=snapshot.production_run_id,
    )


def test_assembly_constructor_has_no_renderer_dependency() -> None:
    parameters = tuple(signature(PublicationAssemblyService).parameters)
    assert parameters == ("artifact_store", "production_artifacts")


@pytest.mark.asyncio
async def test_assembly_persists_exact_v4_body_and_one_publication_artifact() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    run = _run(snapshot)
    service, catalog, artifacts = _service()
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    artifact = await service.assemble_publication(
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    expected_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(document)
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
    assert artifact.input_hash == compute_assembly_input_hash(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    assert artifact.canonical_blob_id == UUID(int=50)
    assert artifact.raw_blob_id is None
    assert artifact.rendered_blob_id is None
    assert artifact.metadata == {}
    document_json = serialize_publication_document(document)
    assert document_json["schema_version"] == PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION
    assert set(document_json) == {
        "schema_version",
        "subject_id",
        "publication_language",
        "title",
        "lead",
        "sections",
        "timeline",
        "indicators",
        "sources",
        "uncertainties",
        "tables",
        "diagrams",
        "figures",
    }


@pytest.mark.asyncio
async def test_assembly_persists_editorial_enrichment_in_the_canonical_body() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence = extraction_evidence_refs_v1(extraction)[0]
    enrichment = _enrichment_citing(extraction, synthesis, evidence)
    service, catalog, _artifacts = _service()
    store = ProductionArtifactStore(catalog)

    artifact = await service.assemble_publication(
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

    first = await service.assemble_publication(**inputs)
    second = await service.assemble_publication(**inputs)

    assert second is first
    assert len(catalog.writes) == 1
    assert artifacts.appended == [first]


@pytest.mark.asyncio
async def test_identical_inputs_reuse_a_finished_run_publication_with_provenance() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    service, catalog, artifacts = _service()
    source_run = _run(snapshot)
    source = await service.assemble_publication(
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
    target_snapshot = replace(snapshot, production_run_id=UUID(int=30))
    target_run = _run(target_snapshot)

    reused = await service.assemble_publication(
        run=target_run,
        snapshot=target_snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=build_empty_editorial_enrichment(
            extraction=extraction, synthesis=synthesis
        ),
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
        "reused": True,
        "reused_from_artifact_id": str(source.id),
        "reused_from_created_at": source.created_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_unreadable_reuse_candidate_falls_back_to_fresh_persistence() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    service, catalog, artifacts = _service()
    source_run = _run(snapshot)
    source = await service.assemble_publication(
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

    fresh = await service.assemble_publication(
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
    first = await service.assemble_publication(
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=build_empty_editorial_enrichment(
            extraction=extraction, synthesis=synthesis
        ),
    )
    changed_synthesis = replace(synthesis, title="Revised synthesis")

    second = await service.assemble_publication(
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
    expected = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=changed_synthesis,
        editorial_enrichment=changed_enrichment,
    )
    assert (first.version, second.version) == (1, 2)
    assert second.input_hash != first.input_hash
    assert second.reused_from_artifact_id is None
    assert second.canonical_blob_id != first.canonical_blob_id
    assert len(catalog.writes) == 2
    assert catalog.writes[1][0] == ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(expected)
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
            ),
        ),
    )


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
    publication_a = await service.assemble_publication(
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

    uow = SimpleNamespace(
        production_artifacts=artifacts,
        production_input_snapshots=Snapshots(),
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
async def test_ioc_repair_requires_enrichment_rebuild_when_cited_evidence_disappears() -> None:
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

    uow = SimpleNamespace(
        production_artifacts=artifacts,
        production_input_snapshots=Snapshots(),
    )
    repair = ProductionRepairMaterializationService(
        lambda: None,  # type: ignore[arg-type]
        projection_service=object(),  # type: ignore[arg-type]
        artifact_store=store,
    )
    with pytest.raises(_EditorialEnrichmentRebuildRequired) as raised:
        await repair._materialize_canonical_publication_in_uow(
            uow,
            run=run,
            extraction=extraction_c_artifact,
            repair_materialization=None,
        )
    assert raised.value.code == "editorial_enrichment_evidence_missing"
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
        await service.assemble_publication(
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

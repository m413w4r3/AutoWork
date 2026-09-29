from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.publication_assembly import PublicationAssemblyService
from cti_app.application.publication_builder import (
    build_publication_document_v3,
    compute_assembly_input_hash,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionFactV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
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
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication import PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION


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

    async def ingest(self, content, *, logical_bucket: str, mime_type: str):
        self.writes.append((content.read(), logical_bucket, mime_type))
        return SimpleNamespace(id=UUID(int=50))


class _RecordingArtifacts:
    def __init__(self) -> None:
        self.appended: list[ProductionArtifact] = []

    async def append(self, artifact: ProductionArtifact) -> None:
        self.appended.append(artifact)

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [artifact for artifact in self.appended if artifact.production_run_id == run_id]


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


@pytest.mark.asyncio
async def test_assembly_persists_exact_v3_body_and_one_publication_artifact() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    run = _run(snapshot)
    service, catalog, artifacts = _service()
    document = build_publication_document_v3(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )

    artifact = await service.assemble_publication(
        run=run,
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    )

    expected_bytes = ProductionArtifactStore.canonical_json_bytes(document.to_json())
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
    )
    assert artifact.canonical_blob_id == UUID(int=50)
    assert artifact.raw_blob_id is None
    assert artifact.rendered_blob_id is None
    assert artifact.metadata == {}
    assert document.to_json()["schema_version"] == PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION
    assert set(document.to_json()) == {
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
    }


@pytest.mark.asyncio
async def test_assembly_appends_next_publication_version_for_the_run() -> None:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    service, _, artifacts = _service()
    inputs = {
        "run": _run(snapshot),
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
    }

    first = await service.assemble_publication(**inputs)
    second = await service.assemble_publication(**inputs)

    assert (first.version, second.version) == (1, 2)
    assert first.input_hash == second.input_hash
    assert artifacts.appended == [first, second]


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
    with pytest.raises(ValueError):
        await service.assemble_publication(
            run=run,
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
        )

    assert catalog.writes == []
    assert artifacts.appended == []

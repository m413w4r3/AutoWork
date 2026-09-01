"""PostgreSQL proof for canonical cross-run production artifact reuse."""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
)
from cti_app.application.edition_workspace import EditionWorkspaceMaterializer
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ConversationResult,
    ModelGateway,
    ModelGatewayError,
    ModelRouter,
    SafeModelRequest,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    ParsedEvent,
    ParsedSource,
    ReferenceReport,
    TechnicalExtraction,
    reference_report_to_json,
    technical_extraction_to_json,
)
from cti_app.application.production_stages import (
    PublicationAssemblyService,
    compute_input_hash,
)
from cti_app.application.production_workflow import (
    ProductionWorkflowOrchestrator,
    _extraction_input_hash,
    _references_input_hash,
    _synthesis_input_hash,
)
from cti_app.application.subject_production import (
    EditionProductionService,
    SubjectProductionService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
)
from cti_app.domain.entities import Subject
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun, ModelUsage
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionReuseInvalidation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.integrations.models import BlobModelOutputStore

pytestmark = pytest.mark.integration


class _CountingRetryModelAdapter:
    """A deterministic bridge-shaped adapter for the real retry workflow."""

    provider = ModelProvider.OPENAI
    requested_model = "fake-production-retry"
    is_external = False

    def __init__(self) -> None:
        self.calls: list[SafeModelRequest] = []
        self._extraction_text = """FACT actors
- Example actor :: The selected campaign
"""
        self._synthesis_text = "The selected campaign was reported [S1]."

    async def invoke(
        self,
        request: SafeModelRequest,
        *,
        role: ModelRole,
        output_schema: object = None,
    ) -> AdapterResult:
        del output_schema
        self.calls.append(request)
        output_text = self._extraction_text if role is ModelRole.RESEARCH else self._synthesis_text
        conversation = request.conversation
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            output_text=output_text,
            conversation=(
                ConversationResult(
                    id=str(conversation.id),
                    mode=conversation.mode,
                    external_locator="https://chatgpt.com/fake-retry",
                    turn_id=f"retry-turn-{len(self.calls)}",
                    verified=True,
                )
                if conversation is not None
                else None
            ),
        )

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: object = None,
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise ModelGatewayError("retry test adapter does not support background responses")


def _edition(
    *, country: str = "France", country_code: str = "FR", target_articles: int = 1
) -> Edition:
    return Edition(
        country=country,
        country_code=country_code,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_articles=target_articles,
        source_profile="test",
        status=EditionStatus.SELECTION,
    )


def _production_context_entities(
    *, edition: Edition, subject: Subject, title: str = "Production subject"
) -> tuple[DiscoveryBatch, EditorialGroup, SourceCandidate]:
    source = SourceCandidate(
        url=f"https://example.test/{subject.slug}",
        title=f"{title} source",
        publisher="Example publisher",
        role=SourceRole.PRIMARY,
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
    )
    candidate = CandidateTopic(
        title=title,
        summary=f"A realistic selected subject for {title}.",
        novelty="new",
        technical_potential=3,
        uncertainties=(),
        relevance_reasons=("test",),
        actors=("Example actor",),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=[source],
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
        actor_or_campaign="Example actor",
    )
    batch = DiscoveryBatch(
        edition_id=edition.id,
        request_hash=(subject.id.hex * 2)[:64],
        complementary_axis="reuse integration",
        queries=(),
        citations=(),
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
        parser_version="test",
        candidates=[candidate],
        source_mode=DiscoverySourceMode.NATIVE_COMPLETE,
        source_coverage_complete=True,
        source_coverage_incomplete_reason=None,
    )
    group = EditorialGroup(
        edition_id=edition.id,
        title=title,
        candidate_references=(CandidateReference(batch.id, candidate.id),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=EditorialScore(
            impact=3,
            novelty=3,
            technical_depth=3,
            hunting_potential=3,
            actionability=3,
            source_quality=3,
            justifications={},
        ),
        source_relationship_status=SourceRelationshipStatus.VERIFIED,
        needs_source_verification=False,
        needs_source_expansion=False,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="A stable functional subject for reuse.",
    )
    group.select(subject.id)
    return batch, group, source


async def _seed_computed_run(
    uow_factory: UnitOfWorkFactory,
    store: ProductionArtifactStore,
    *,
    edition: Edition,
    subject: Subject,
    created_at: datetime,
) -> tuple[SubjectProductionRun, dict[ProductionArtifactStage, ProductionArtifact]]:
    # The logical edition repository may replace a freshly generated ID with
    # an existing edition's ID, so persist these parents before constructing
    # the run that references the final edition ID.
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.commit()

    run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        created_at=created_at,
        updated_at=created_at,
    )
    refs_blobs = await store.store_stage_payloads(
        raw="references raw",
        canonical={"stage": "references"},
    )
    extraction_blobs = await store.store_stage_payloads(
        raw="extraction raw",
        canonical={"stage": "extraction"},
    )
    synthesis_blobs = await store.store_stage_payloads(
        raw="synthesis raw",
        rendered="synthesis rendered",
    )
    blobs = {
        ProductionArtifactStage.REFERENCES: refs_blobs,
        ProductionArtifactStage.EXTRACTION: extraction_blobs,
        ProductionArtifactStage.SYNTHESIS: synthesis_blobs,
    }
    hashes = {
        ProductionArtifactStage.REFERENCES: "a" * 64,
        ProductionArtifactStage.EXTRACTION: "b" * 64,
        ProductionArtifactStage.SYNTHESIS: "c" * 64,
    }
    artifacts = {
        stage: ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=stage,
            version=1,
            input_hash=hashes[stage],
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=payloads[0],
            canonical_blob_id=payloads[1],
            rendered_blob_id=payloads[2],
            created_at=created_at,
        )
        for stage, payloads in blobs.items()
    }
    async with uow_factory() as uow:
        await uow.subject_production_runs.add(run)
        for artifact in artifacts.values():
            await uow.production_artifacts.append(artifact)
        await uow.commit()
    return run, artifacts


async def _seed_reusable_article(
    uow_factory: UnitOfWorkFactory,
    store: ProductionArtifactStore,
    *,
    edition: Edition,
    subject: Subject,
    title: str,
) -> tuple[
    SubjectProductionRun,
    dict[ProductionArtifactStage, ProductionArtifact],
    ProductionArtifact,
]:
    """Create one complete first pass whose costly inputs can be reused."""
    batch, group, source = _production_context_entities(
        edition=edition, subject=subject, title=title
    )
    discovery_model_run = ModelRun(
        id=batch.discovery_model_run_id,
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake",
        prompt_template_id="integration",
        prompt_template_version="1",
        authorized_input_hash=(subject.id.hex * 2)[:64],
        evidence_pack_hash=(batch.id.hex * 2)[:64],
        parameters={},
    )
    collection = SourceCollection(
        subject_id=subject.id,
        edition_id=edition.id,
        group_id=group.id,
        batch_id=batch.id,
        source_candidate_id=source.id,
        requested_url=source.url,
        canonical_url=source.canonical_url,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        source_tlp=source.tlp,
        sensitivity=source.sensitivity,
        external_llm_allowed=True,
        proposed_role=source.role,
        origin_kind=SourceOriginKind.DISCOVERY,
        state=CollectionState.ARCHIVED,
    )
    async with uow_factory() as uow:
        await uow.model_runs.add(discovery_model_run)
        assert await uow.discovery_batches.add_if_absent(batch)
        await uow.editorial_groups.add(group)
        assert await uow.source_collections.add_if_absent(collection)
        await uow.commit()

    production = SubjectProductionService(uow_factory)
    source_run, created = await production.create_run(subject.id, edition.id)
    assert created
    source_run = await production.start_run(source_run.id)
    async with uow_factory() as uow:
        persisted = await uow.subject_production_runs.get_for_update(source_run.id)
        assert persisted is not None
        persisted.current_stage = SubjectProductionStage.ASSEMBLY
        persisted.mark_ready()
        await uow.subject_production_runs.save(persisted)
        await uow.commit()
        snapshot = await uow.production_input_snapshots.get_by_run(source_run.id)
    assert snapshot is not None

    report = ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title=source.title,
                url=source.canonical_url,
                canonical_url=source.canonical_url,
                publisher=source.publisher,
                published_at=source.published_at,
                role=source.role,
            ),
        ),
        events=(
            ParsedEvent(
                local_id="R1",
                event_date=date(2026, 8, 5),
                source_ids=("S1",),
                text=f"{title} was reported.",
            ),
        ),
    )
    extraction = TechnicalExtraction(items=())
    refs_hash = _references_input_hash(
        subject_id=subject.id,
        snapshot=snapshot,
        subject_title=snapshot.subject_title,
        subject_description=snapshot.subject_description,
        research_date=snapshot.research_date,
    )
    refs_payload = reference_report_to_json(report)
    extraction_payload = technical_extraction_to_json(extraction)
    extraction_hash = _extraction_input_hash(
        subject_id=subject.id,
        references_hash=refs_hash,
        source_urls=[source.canonical_url],
        references_payload_hash=compute_input_hash(refs_payload),
    )
    synthesis_pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        report, extraction, {source.canonical_url: "core"}
    )
    synthesis_hash = _synthesis_input_hash(
        subject_id=subject.id,
        references_hash=refs_hash,
        reference_report_hash=compute_input_hash(refs_payload),
        extraction_hash=extraction_hash,
        technical_extraction_hash=compute_input_hash(extraction_payload),
        synthesis_evidence_pack_hash=compute_input_hash(synthesis_pack),
    )

    refs_raw_id, refs_blob_id, _ = await store.store_stage_payloads(
        raw=f"{title} references", canonical=refs_payload
    )
    extraction_raw_id, extraction_blob_id, _ = await store.store_stage_payloads(
        raw=f"{title} extraction", canonical=extraction_payload
    )
    _, _, synthesis_blob_id = await store.store_stage_payloads(
        raw=f"{title} synthesis", rendered=f"{title} was reported [S1]."
    )
    source_artifacts = {
        ProductionArtifactStage.REFERENCES: ProductionArtifact(
            production_run_id=source_run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash=refs_hash,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=refs_raw_id,
            canonical_blob_id=refs_blob_id,
        ),
        ProductionArtifactStage.EXTRACTION: ProductionArtifact(
            production_run_id=source_run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash=extraction_hash,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=extraction_raw_id,
            canonical_blob_id=extraction_blob_id,
        ),
        ProductionArtifactStage.SYNTHESIS: ProductionArtifact(
            production_run_id=source_run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash=synthesis_hash,
            status=ProductionArtifactStatus.VERIFIED,
            rendered_blob_id=synthesis_blob_id,
        ),
    }
    async with uow_factory() as uow:
        for artifact in source_artifacts.values():
            await uow.production_artifacts.append(artifact)
        await uow.commit()

    publication = await PublicationAssemblyService(uow_factory, store).assemble_publication(
        run_id=source_run.id,
        subject_id=subject.id,
        subject_title=title,
        references_artifact=source_artifacts[ProductionArtifactStage.REFERENCES],
        extraction_artifact=source_artifacts[ProductionArtifactStage.EXTRACTION],
        synthesis_artifact=source_artifacts[ProductionArtifactStage.SYNTHESIS],
    )
    return source_run, source_artifacts, publication


@pytest.mark.asyncio
async def test_postgres_run_b_reuses_all_costly_artifacts_from_run_a(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition = _edition()
    subject = Subject(
        external_id=f"SUBJ-REUSE-{uuid4()}",
        slug=f"reuse-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    catalog = BlobCatalogService(blob_store, uow_factory)
    store = ProductionArtifactStore(catalog)
    created_at = datetime.now(UTC) - timedelta(minutes=1)
    source_run, source_artifacts = await _seed_computed_run(
        uow_factory,
        store,
        edition=edition,
        subject=subject,
        created_at=created_at,
    )
    target_run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        run_number=2,
        status=SubjectProductionStatus.RUNNING,
        current_stage=SubjectProductionStage.REFERENCES,
    )
    async with uow_factory() as uow:
        await uow.subject_production_runs.add(target_run)
        await uow.commit()

    service = ProductionArtifactReuseService(uow_factory, store)
    for stage, source in source_artifacts.items():
        result = await service.find_or_reuse(
            run=target_run,
            stage=stage,
            input_hash=source.input_hash,
        )
        assert result is not None
        assert result.reused is True
        assert result.artifact.production_run_id == target_run.id
        assert result.artifact.production_run_id != source_run.id
        assert result.artifact.reused_from_artifact_id == source.id
        assert result.artifact.raw_blob_id == source.raw_blob_id
        assert result.artifact.canonical_blob_id == source.canonical_blob_id
        assert result.artifact.rendered_blob_id == source.rendered_blob_id

    async with uow_factory() as uow:
        source_rows = await uow.production_artifacts.list_for_run(source_run.id)
        target_rows = await uow.production_artifacts.list_for_run(target_run.id)
    assert {row.id for row in source_rows} == {
        artifact.id for artifact in source_artifacts.values()
    }
    assert {row.stage for row in target_rows} == set(source_artifacts)
    assert all(row.reused_from_artifact_id is not None for row in target_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_stage", "references_allowed", "extraction_allowed"),
    (
        (SubjectProductionStage.REFERENCES, False, False),
        (SubjectProductionStage.EXTRACTION, True, False),
        (SubjectProductionStage.SYNTHESIS, True, True),
    ),
)
async def test_postgres_invalidation_blocks_only_downstream_stages(
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
    from_stage: SubjectProductionStage,
    references_allowed: bool,
    extraction_allowed: bool,
) -> None:
    edition = _edition()
    subject = Subject(
        external_id=f"SUBJ-INVALIDATE-{uuid4()}",
        slug=f"invalidate-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    source_run, source_artifacts = await _seed_computed_run(
        uow_factory,
        store,
        edition=edition,
        subject=subject,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    occurred_at = datetime.now(UTC) - timedelta(minutes=1)
    async with uow_factory() as uow:
        await uow.production_reuse_invalidations.add(
            ProductionReuseInvalidation(
                edition_id=edition.id,
                subject_id=subject.id,
                from_stage=from_stage,
                actor_id="operator",
                correlation_id=str(uuid4()),
                occurred_at=occurred_at,
            )
        )
        target_run = SubjectProductionRun(
            subject_id=subject.id,
            edition_id=edition.id,
            run_number=2,
            status=SubjectProductionStatus.RUNNING,
            current_stage=SubjectProductionStage.REFERENCES,
        )
        await uow.subject_production_runs.add(target_run)
        await uow.commit()

    service = ProductionArtifactReuseService(uow_factory, store)
    references = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.REFERENCES,
        input_hash=source_artifacts[ProductionArtifactStage.REFERENCES].input_hash,
    )
    extraction = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.EXTRACTION,
        input_hash=source_artifacts[ProductionArtifactStage.EXTRACTION].input_hash,
    )
    synthesis = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.SYNTHESIS,
        input_hash=source_artifacts[ProductionArtifactStage.SYNTHESIS].input_hash,
    )

    assert (references is not None) is references_allowed
    assert (extraction is not None) is extraction_allowed
    assert synthesis is None
    async with uow_factory() as uow:
        old_rows = await uow.production_artifacts.list_for_run(source_run.id)
    assert len(old_rows) == 3


@pytest.mark.asyncio
async def test_real_orchestrator_reuses_run_a_then_freezes_run_b_identity(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """Run the canonical A -> B proof through the real SQL UoW and orchestrator."""
    edition = _edition(country="Germany", country_code="DE")
    subject = Subject(
        external_id=f"SUBJ-ORCHESTRATOR-{uuid4()}",
        slug=f"orchestrator-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    catalog = BlobCatalogService(blob_store, uow_factory)
    store = ProductionArtifactStore(catalog)

    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.commit()

    batch, group, source = _production_context_entities(edition=edition, subject=subject)
    discovery_model_run = ModelRun(
        id=batch.discovery_model_run_id,
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake",
        prompt_template_id="integration",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
    )
    collection = SourceCollection(
        subject_id=subject.id,
        edition_id=edition.id,
        group_id=group.id,
        batch_id=batch.id,
        source_candidate_id=source.id,
        requested_url=source.url,
        canonical_url=source.canonical_url,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        source_tlp=source.tlp,
        sensitivity=source.sensitivity,
        external_llm_allowed=True,
        proposed_role=source.role,
        origin_kind=SourceOriginKind.DISCOVERY,
        state=CollectionState.ARCHIVED,
    )

    async with uow_factory() as uow:
        await uow.model_runs.add(discovery_model_run)
        assert await uow.discovery_batches.add_if_absent(batch)
        await uow.editorial_groups.add(group)
        assert await uow.source_collections.add_if_absent(collection)
        await uow.commit()

    production = SubjectProductionService(uow_factory)
    run_a, created_a = await production.create_run(subject.id, edition.id)
    assert created_a
    run_a = await production.start_run(run_a.id)
    async with uow_factory() as uow:
        persisted_a = await uow.subject_production_runs.get_for_update(run_a.id)
        assert persisted_a is not None
        persisted_a.current_stage = SubjectProductionStage.ASSEMBLY
        persisted_a.mark_ready()
        await uow.subject_production_runs.save(persisted_a)
        await uow.commit()
        snapshot_a = await uow.production_input_snapshots.get_by_run(run_a.id)
    assert snapshot_a is not None

    report = ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title=source.title,
                url=source.canonical_url,
                canonical_url=source.canonical_url,
                publisher=source.publisher,
                published_at=source.published_at,
                role=source.role,
            ),
        ),
        events=(
            ParsedEvent(
                local_id="R1",
                event_date=date(2026, 8, 5),
                source_ids=("S1",),
                text="The selected campaign was reported.",
            ),
        ),
    )
    extraction = TechnicalExtraction(items=())
    refs_hash = _references_input_hash(
        subject_id=subject.id,
        snapshot=snapshot_a,
        subject_title=snapshot_a.subject_title,
        subject_description=snapshot_a.subject_description,
        research_date=snapshot_a.research_date,
    )
    refs_payload = reference_report_to_json(report)
    extraction_payload = technical_extraction_to_json(extraction)
    extraction_hash = _extraction_input_hash(
        subject_id=subject.id,
        references_hash=refs_hash,
        source_urls=[source.canonical_url],
        references_payload_hash=compute_input_hash(refs_payload),
    )
    synthesis_pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        report, extraction, {source.canonical_url: "core"}
    )
    synthesis_hash = _synthesis_input_hash(
        subject_id=subject.id,
        references_hash=refs_hash,
        reference_report_hash=compute_input_hash(refs_payload),
        extraction_hash=extraction_hash,
        technical_extraction_hash=compute_input_hash(extraction_payload),
        synthesis_evidence_pack_hash=compute_input_hash(synthesis_pack),
    )

    refs_raw_id, refs_blob_id, _ = await store.store_stage_payloads(
        raw="references A", canonical=refs_payload
    )
    extraction_raw_id, extraction_blob_id, _ = await store.store_stage_payloads(
        raw="extraction A", canonical=extraction_payload
    )
    _, _, synthesis_blob_id = await store.store_stage_payloads(
        raw="synthesis A", rendered="The selected campaign was reported [S1]."
    )
    source_artifacts = {
        ProductionArtifactStage.REFERENCES: ProductionArtifact(
            production_run_id=run_a.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash=refs_hash,
            raw_blob_id=refs_raw_id,
            canonical_blob_id=refs_blob_id,
        ),
        ProductionArtifactStage.EXTRACTION: ProductionArtifact(
            production_run_id=run_a.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash=extraction_hash,
            raw_blob_id=extraction_raw_id,
            canonical_blob_id=extraction_blob_id,
        ),
        ProductionArtifactStage.SYNTHESIS: ProductionArtifact(
            production_run_id=run_a.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash=synthesis_hash,
            rendered_blob_id=synthesis_blob_id,
        ),
    }
    async with uow_factory() as uow:
        for artifact in source_artifacts.values():
            await uow.production_artifacts.append(artifact)
        await uow.commit()

    assembly = PublicationAssemblyService(uow_factory, store)
    publication_a = await assembly.assemble_publication(
        run_id=run_a.id,
        subject_id=subject.id,
        subject_title=snapshot_a.subject_title,
        references_artifact=source_artifacts[ProductionArtifactStage.REFERENCES],
        extraction_artifact=source_artifacts[ProductionArtifactStage.EXTRACTION],
        synthesis_artifact=source_artifacts[ProductionArtifactStage.SYNTHESIS],
    )

    run_b, created_b = await production.create_run(subject.id, edition.id)
    assert created_b
    assert run_b.id != run_a.id
    assert run_b.run_number == run_a.run_number + 1
    async with uow_factory() as uow:
        snapshot_b = await uow.production_input_snapshots.get_by_run(run_b.id)
    assert snapshot_b is not None
    assert snapshot_b.reuse_basis_hash == snapshot_a.reuse_basis_hash
    assert snapshot_b.research_date == snapshot_a.research_date
    assert snapshot_b.input_hash == snapshot_a.input_hash

    await production.start_run(run_b.id)
    await production.advance_stage(run_b.id)

    class SentinelModelGateway:
        async def execute(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("model must not be called on reuse hit")

    orchestrator = ProductionWorkflowOrchestrator(
        uow_factory,
        model_gateway=SentinelModelGateway(),  # type: ignore[arg-type]
        artifact_store=store,
    )
    for stage in (
        SubjectProductionStage.REFERENCES,
        SubjectProductionStage.EXTRACTION,
        SubjectProductionStage.SYNTHESIS,
    ):
        result = await orchestrator.execute_stage(run_b.id, stage)
        assert result["status"] == "reused"
        if stage is not SubjectProductionStage.SYNTHESIS:
            await production.advance_stage(run_b.id)

    await production.advance_stage(run_b.id)
    assembly_result = await orchestrator.execute_stage(run_b.id, SubjectProductionStage.ASSEMBLY)
    assert assembly_result["status"] == "success"

    async with uow_factory() as uow:
        artifacts_b = {
            artifact.stage: artifact
            for artifact in await uow.production_artifacts.list_for_run(run_b.id)
        }
        persisted_b = await uow.subject_production_runs.get(run_b.id)
    assert persisted_b is not None
    assert persisted_b.status is SubjectProductionStatus.READY
    for stage, source_artifact in source_artifacts.items():
        reused = artifacts_b[stage]
        assert reused.id != source_artifact.id
        assert reused.production_run_id == run_b.id
        assert reused.reused_from_artifact_id == source_artifact.id
        assert reused.canonical_blob_id == source_artifact.canonical_blob_id
        assert reused.rendered_blob_id == source_artifact.rendered_blob_id
    publication_b = artifacts_b[ProductionArtifactStage.PUBLICATION]
    assert publication_b.production_run_id == run_b.id
    assert publication_b.status is ProductionArtifactStatus.VERIFIED
    assert publication_b.reused_from_artifact_id is None
    assert publication_b.id != publication_a.id

    async with uow_factory() as uow:
        edition_before_retry = await uow.editions.get_for_update(edition.id)
        assert edition_before_retry is not None
        assert await uow.publication_manifests.get_latest_for_edition(edition.id) is None
        if edition_before_retry.status is EditionStatus.SELECTION:
            expected_version = edition_before_retry.version
            edition_before_retry.transition(EditionStatus.PRODUCTION)
            assert await uow.editions.update(edition_before_retry, expected_version)
        else:
            assert edition_before_retry.status is EditionStatus.PRODUCTION
        await uow.commit()

    retry = await production.retry_from_stage(run_b.id, SubjectProductionStage.EXTRACTION)
    assert retry.previous_status is SubjectProductionStatus.READY
    assert retry.run.status is SubjectProductionStatus.RUNNING
    assert retry.run.current_stage is SubjectProductionStage.EXTRACTION
    assert retry.run.pipeline_generation == persisted_b.pipeline_generation + 1
    assert retry.run.force_recompute_from_stage is SubjectProductionStage.EXTRACTION
    assert retry.staled_artifacts == ["extraction", "synthesis", "publication"]

    async with uow_factory() as uow:
        stale_artifacts = {
            artifact.stage: artifact
            for artifact in await uow.production_artifacts.list_for_run(run_b.id)
        }
    assert (
        stale_artifacts[ProductionArtifactStage.REFERENCES].status
        is ProductionArtifactStatus.VERIFIED
    )
    assert (
        stale_artifacts[ProductionArtifactStage.EXTRACTION].status is ProductionArtifactStatus.STALE
    )
    assert (
        stale_artifacts[ProductionArtifactStage.SYNTHESIS].status is ProductionArtifactStatus.STALE
    )
    assert (
        stale_artifacts[ProductionArtifactStage.PUBLICATION].status
        is ProductionArtifactStatus.STALE
    )

    retry_adapter = _CountingRetryModelAdapter()
    retry_router = ModelRouter(
        openai_research=retry_adapter,
        openai_structured=retry_adapter,
        openai_drafting=retry_adapter,
        qwen=retry_adapter,
        fake=retry_adapter,
    )
    retry_gateway = ModelGateway(
        retry_router,
        uow_factory,
        BlobModelOutputStore(catalog),
    )
    retry_conversations = ModelConversationService(uow_factory, retry_gateway, blob_store)
    retry_orchestrator = ProductionWorkflowOrchestrator(
        uow_factory,
        model_service=retry_conversations,
        model_gateway=retry_gateway,
        artifact_store=store,
    )

    extraction_retry = await retry_orchestrator.execute_stage(
        run_b.id, SubjectProductionStage.EXTRACTION
    )
    assert extraction_retry["status"] == "success"
    extraction_technical_replay = await retry_orchestrator.execute_stage(
        run_b.id, SubjectProductionStage.EXTRACTION
    )
    assert extraction_technical_replay["status"] == "cached"
    assert len(retry_adapter.calls) == 1
    await production.advance_stage(run_b.id)

    synthesis_retry = await retry_orchestrator.execute_stage(
        run_b.id, SubjectProductionStage.SYNTHESIS
    )
    assert synthesis_retry["status"] == "success"
    synthesis_technical_replay = await retry_orchestrator.execute_stage(
        run_b.id, SubjectProductionStage.SYNTHESIS
    )
    assert synthesis_technical_replay["status"] == "cached"
    assert len(retry_adapter.calls) == 2
    await production.advance_stage(run_b.id)
    retry_assembly = await retry_orchestrator.execute_stage(
        run_b.id, SubjectProductionStage.ASSEMBLY
    )
    assert retry_assembly["status"] == "success"

    async with uow_factory() as uow:
        artifacts_b = {
            artifact.stage: artifact
            for artifact in await uow.production_artifacts.list_for_run(run_b.id)
        }
        persisted_b = await uow.subject_production_runs.get(run_b.id)
    assert persisted_b is not None
    assert persisted_b.status is SubjectProductionStatus.READY
    assert artifacts_b[ProductionArtifactStage.REFERENCES].id == (
        stale_artifacts[ProductionArtifactStage.REFERENCES].id
    )
    assert artifacts_b[ProductionArtifactStage.EXTRACTION].id != (
        stale_artifacts[ProductionArtifactStage.EXTRACTION].id
    )
    assert artifacts_b[ProductionArtifactStage.SYNTHESIS].id != (
        stale_artifacts[ProductionArtifactStage.SYNTHESIS].id
    )
    assert artifacts_b[ProductionArtifactStage.EXTRACTION].reused_from_artifact_id is None
    assert artifacts_b[ProductionArtifactStage.SYNTHESIS].reused_from_artifact_id is None
    publication_b = artifacts_b[ProductionArtifactStage.PUBLICATION]
    assert publication_b.production_run_id == run_b.id
    assert publication_b.status is ProductionArtifactStatus.VERIFIED
    assert publication_b.reused_from_artifact_id is None

    review_batch = EditionProductionBatch(
        edition_id=edition.id,
        status="running",
        phase=ProductionBatchPhase.REVIEW,
    )
    review_item = EditionProductionBatchItem(
        batch_id=review_batch.id,
        subject_id=subject.id,
        production_run_id=run_b.id,
        position=1,
    )
    async with uow_factory() as uow:
        await uow.edition_production_batches.add(review_batch)
        await uow.commit()

    async with uow_factory() as uow:
        await uow.edition_production_batch_items.append_many((review_item,))
        locked_edition = await uow.editions.get_for_update(edition.id)
        assert locked_edition is not None
        expected_version = locked_edition.version
        locked_edition.transition(EditionStatus.REVIEW)
        assert await uow.editions.update(locked_edition, expected_version)
        await uow.commit()

    async with uow_factory() as uow:
        review_rows = await uow.edition_review_read_model.list_for_edition(edition.id)
    assert len(review_rows) == 1
    assert review_rows[0].run_id == run_b.id
    assert review_rows[0].pipeline_generation == persisted_b.pipeline_generation
    assert review_rows[0].document_artifact_id == publication_b.id

    accepted = await EditionPublicationService(uow_factory, store).accept(
        edition.id, actor_id="reviewer", correlation_id="reuse-integration"
    )
    assert accepted.manifest.entries[0].production_run_id == run_b.id
    assert accepted.manifest.entries[0].pipeline_generation == persisted_b.pipeline_generation
    assert accepted.manifest.entries[0].document_artifact_id == publication_b.id
    assert accepted.manifest.entries[0].document_artifact_id != publication_a.id


@pytest.mark.asyncio
async def test_two_article_cached_edition_is_sequential_and_uses_new_publications(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """Mirror the low-cost manual batch with two real PostgreSQL-backed runs."""
    edition = _edition(country="Italy", country_code="IT", target_articles=2)
    subjects = [
        Subject(
            external_id=f"SUBJ-TWO-ARTICLE-{label}-{uuid4()}",
            slug=f"two-article-{label.lower()}-{uuid4().hex}",
            tlp=TLP.AMBER,
        )
        for label in ("A", "B")
    ]
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        for subject in subjects:
            await uow.subjects.add(subject)
        await uow.commit()

    source_publications: list[ProductionArtifact] = []
    for subject, title in zip(subjects, ("Article A", "Article B"), strict=True):
        _source_run, _, source_publication = await _seed_reusable_article(
            uow_factory,
            store,
            edition=edition,
            subject=subject,
            title=title,
        )
        source_publications.append(source_publication)

    batch_service = EditionProductionService(uow_factory)
    batch = await batch_service.create_batch(edition.id, [subject.id for subject in subjects])
    first = await batch_service.start_next(batch.id)
    assert first is not None
    assert first.subject_id == subjects[0].id

    async with uow_factory() as uow:
        batch_items = await uow.edition_production_batch_items.list_for_batch(batch.id)
        queued_runs = [
            await uow.subject_production_runs.get(item.production_run_id) for item in batch_items
        ]
    assert [run.status for run in queued_runs if run is not None] == [
        SubjectProductionStatus.RUNNING,
        SubjectProductionStatus.QUEUED,
    ]
    second_id = batch_items[1].production_run_id

    class SentinelModelGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            self.calls += 1
            raise AssertionError("no model call expected during cached edition smoke")

    sentinel = SentinelModelGateway()
    orchestrator = ProductionWorkflowOrchestrator(
        uow_factory,
        model_gateway=sentinel,  # type: ignore[arg-type]
        artifact_store=store,
    )
    cached_results: list[dict[str, object]] = []

    async def execute_cached(run_id: UUID) -> None:
        await SubjectProductionService(uow_factory).advance_stage(run_id)
        for stage in (
            SubjectProductionStage.REFERENCES,
            SubjectProductionStage.EXTRACTION,
            SubjectProductionStage.SYNTHESIS,
        ):
            result = await orchestrator.execute_stage(run_id, stage)
            cached_results.append(result)
            assert result["status"] == "reused"
            if stage is not SubjectProductionStage.SYNTHESIS:
                await SubjectProductionService(uow_factory).advance_stage(run_id)
        await SubjectProductionService(uow_factory).advance_stage(run_id)
        assembly_result = await orchestrator.execute_stage(run_id, SubjectProductionStage.ASSEMBLY)
        assert assembly_result["status"] == "success"

    await execute_cached(first.id)
    second = await batch_service.on_subject_terminal(batch.id, first.id)
    assert second is not None
    assert second.id == second_id
    assert second.status is SubjectProductionStatus.RUNNING
    await execute_cached(second.id)
    assert await batch_service.on_subject_terminal(batch.id, second.id) is None
    assert len(cached_results) == 6
    assert sentinel.calls == 0

    async with uow_factory() as uow:
        persisted_edition = await uow.editions.get(edition.id)
        review_rows = await uow.edition_review_read_model.list_for_edition(edition.id)
        target_publications = []
        for row in review_rows:
            artifacts = await uow.production_artifacts.list_for_run(row.run_id)
            target_publications.append(
                next(
                    artifact
                    for artifact in artifacts
                    if artifact.stage is ProductionArtifactStage.PUBLICATION
                    and artifact.status is ProductionArtifactStatus.VERIFIED
                )
            )
    assert persisted_edition is not None
    assert persisted_edition.status is EditionStatus.REVIEW
    assert [row.position for row in review_rows] == [1, 2]
    assert [row.run_id for row in review_rows] == [first.id, second.id]
    assert [artifact.id for artifact in target_publications] != [
        publication.id for publication in source_publications
    ]

    accepted = await EditionPublicationService(uow_factory, store).accept(
        edition.id, actor_id="reviewer", correlation_id="two-article-cached-smoke"
    )
    assert [entry.production_run_id for entry in accepted.manifest.entries] == [
        first.id,
        second.id,
    ]
    assert [entry.document_artifact_id for entry in accepted.manifest.entries] == [
        artifact.id for artifact in target_publications
    ]

    release = await EditionAssemblyService(
        uow_factory,
        store,
        workspace_materializer=EditionWorkspaceMaterializer(tmp_path / "editions"),
    ).assemble(accepted.manifest.id)
    edition_json = await store.read_json(release.edition_document_blob_id)
    assert [item["document"]["title"] for item in edition_json["publications"]] == [
        "[Publication] Article A",
        "[Publication] Article B",
    ]
    docx = await store.read_bytes(release.docx_blob_id, max_bytes=32 * 1024 * 1024)
    with zipfile.ZipFile(BytesIO(docx)) as archive:
        document_xml = archive.read("word/document.xml")
    assert document_xml.index(b"Article A") < document_xml.index(b"Article B")

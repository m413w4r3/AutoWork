"""PostgreSQL persistence proof for the append-only addition journal."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.collection import SubjectCollectionService, SupplementalSource
from cti_app.application.http_collection import (
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import (
    ExtractionExecutionStatus,
    ProductionExtractionService,
)
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2FactProposal,
    Q2SourceOutput,
)
from cti_app.application.production_references import (
    build_production_reference_corpus,
    observe_reference_collections,
    production_reference_corpus_from_json,
    production_reference_corpus_to_json,
)
from cti_app.application.production_relevance import (
    build_relevance_projection,
    relevance_projection_from_json,
    relevance_projection_to_json,
)
from cti_app.application.production_source_additions import (
    SourceAdditionRefusalCode,
    SourceAdditionService,
)
from cti_app.application.production_synthesis import (
    SynthesisClaimProposalV1,
    SynthesisProposalV1,
    SynthesisSectionProposalV1,
    build_synthesis_evidence_pack,
    extraction_evidence_refs_v1,
)
from cti_app.application.production_synthesis_revision import (
    TargetedSynthesisRevisionValidationError,
    _assert_claim_handles_and_relations_supported,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.discovery import SourceRole
from cti_app.domain.discovery_cumulative import (
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
)
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    SOURCE_ADDITION_EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
    SourceAdditionExtractionIntent,
)
from cti_app.domain.production_extraction import (
    ExtractionProfileReasonCode,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    extraction_profile_decision,
    production_extraction_from_json,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.domain.production_relevance import RelevanceClassification
from cti_app.domain.production_source_additions import (
    SourceAdditionJournalEntryV1,
    SourceAdditionOrigin,
    SourceAdditionState,
    SourceAdditionV1,
    source_addition_source_key,
)
from cti_app.domain.production_synthesis import EvidenceKind, SynthesisSectionKind
from cti_app.domain.selection import (
    SelectionAction,
    SelectionDecision,
    SubjectDiscoveryOrigin,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.test_production_extraction_service import _StructuredGateway

from .edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_source_addition_repository_round_trips_immutable_versions(
    uow_factory: UnitOfWorkFactory,
) -> None:
    country_code = reserve_edition_code()
    edition = Edition(
        country=f"Additionland {country_code}",
        country_code=country_code,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Source addition repository test",
        slug=f"source-addition-{uuid4().hex[:12]}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.commit()

    request_id = uuid4()
    addition = SourceAdditionV1(
        addition_id=uuid4(),
        request_id=request_id,
        production_run_id=run.id,
        subject_id=subject.id,
        state=SourceAdditionState.CANDIDATE,
        origin=SourceAdditionOrigin.OPERATOR_URL,
        canonical_url="https://example.test/report",
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
    )
    selected = replace(
        addition,
        state=SourceAdditionState.SELECTED,
        selection_criterion="Relevant to the current case",
        selection_criterion_version="criterion-v1",
        selected_by="analyst@example.test",
    )
    async with uow_factory() as uow:
        await uow.production_source_additions.append(
            SourceAdditionJournalEntryV1(addition=addition, version=1)
        )
        await uow.production_source_additions.append(
            SourceAdditionJournalEntryV1(addition=selected, version=2)
        )
        await uow.commit()

    async with uow_factory() as uow:
        latest = await uow.production_source_additions.get_latest_for_identity(
            run.id, request_id, source_addition_source_key(addition.canonical_url)
        )
        listed = await uow.production_source_additions.list_latest_for_request(run.id, request_id)

    assert latest == SourceAdditionJournalEntryV1(addition=selected, version=2)
    assert listed == [latest]


@pytest.mark.asyncio
async def test_source_addition_and_promotion_artifact_rollback_in_one_postgres_uow(
    uow_factory: UnitOfWorkFactory,
) -> None:
    country_code = reserve_edition_code()
    edition = Edition(
        country=f"Additionland {country_code}",
        country_code=country_code,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Source addition transaction test",
        slug=f"source-addition-rollback-{uuid4().hex[:12]}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.commit()
    async with uow_factory() as uow:
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.commit()

    request_id = uuid4()
    addition = SourceAdditionV1(
        addition_id=uuid4(),
        request_id=request_id,
        production_run_id=run.id,
        subject_id=subject.id,
        state=SourceAdditionState.CANDIDATE,
        origin=SourceAdditionOrigin.OPERATOR_URL,
        canonical_url="https://example.test/nanodump",
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
    )
    source_additions_hash_value = "b" * 64
    manifest = [
        {
            "request_id": str(request_id),
            "url": addition.canonical_url,
            "source_document_id": str(uuid4()),
            "content_sha256": "c" * 64,
        }
    ]
    promoted_artifacts = tuple(
        ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=stage,
            version=1,
            input_hash=("d", "e", "f", "0")[index] * 64,
            metadata={
                "source_additions_hash": source_additions_hash_value,
                "source_additions": manifest,
                **(
                    {"revision": {"source_additions": manifest}}
                    if stage is ProductionArtifactStage.SYNTHESIS
                    else {}
                ),
            },
        )
        for index, stage in enumerate(
            (
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.RELEVANCE_PROJECTION,
                ProductionArtifactStage.SYNTHESIS,
            )
        )
    )
    with pytest.raises(RuntimeError, match="revision failed after promotion"):
        async with uow_factory() as uow:
            await uow.production_source_additions.append(
                SourceAdditionJournalEntryV1(addition=addition, version=1)
            )
            for artifact in promoted_artifacts:
                await uow.production_artifacts.append(artifact)
            raise RuntimeError("revision failed after promotion")

    async with uow_factory() as uow:
        latest = await uow.production_source_additions.get_latest_for_identity(
            run.id, request_id, source_addition_source_key(addition.canonical_url)
        )
        artifacts = {
            stage: await uow.production_artifacts.get_current(run.id, stage.value)
            for stage in (
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.RELEVANCE_PROJECTION,
                ProductionArtifactStage.SYNTHESIS,
            )
        }

    assert latest is None
    assert all(artifact is None for artifact in artifacts.values())


class _NoNetworkResolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return ("93.184.216.34",)


class _NoNetworkTransport:
    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        raise AssertionError(f"Manual acquisition attempted HTTP for {request.url}")


async def _seed_real_source_addition_service(
    *,
    uow_factory: UnitOfWorkFactory,
    tmp_path,
) -> SimpleNamespace:
    unique = uuid4()
    country_code = reserve_edition_code()
    edition = Edition(
        country=f"Additionland {country_code}",
        country_code=country_code,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        tlp=TLP.CLEAR,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="NanoDump source addition integration",
        slug=f"nanodump-source-addition-{unique.hex}",
        tlp=TLP.CLEAR,
    )
    research_date = date(2026, 10, 1)
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        research_date=research_date,
    )
    merge_run = DiscoveryMergeRun(
        edition_id=edition.id,
        parent_snapshot_id=None,
        intake_id=None,
        planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
        prompt_version="1",
        policy_version="1",
        blocking_version="1",
        merge_input_hash=hashlib.sha256(subject.id.bytes).hexdigest(),
        handle_map={},
        included_subject_ids=(subject.id,),
        excluded_subject_count=0,
        validation_status=MergeValidationStatus.VALID,
    )
    discovery_snapshot = DiscoverySnapshot(
        edition_id=edition.id,
        version=1,
        parent_snapshot_id=None,
        intake_id=None,
        merge_run_id=merge_run.id,
        planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
        subjects=(),
        snapshot_hash=hashlib.sha256(subject.id.bytes).hexdigest(),
        is_active=True,
    )
    decision = SelectionDecision(
        edition_id=edition.id,
        discovery_subject_id=subject.id,
        snapshot_id=discovery_snapshot.id,
        snapshot_version=discovery_snapshot.version,
        action=SelectionAction.SELECT,
        subject_id=subject.id,
        actor_id="source-addition-integration",
        correlation_id="source-addition-integration",
        idempotency_key=f"source-addition-integration-{subject.id}",
    )
    discovery_candidate_id = uuid4()
    core_source = ProductionInputSource(
        discovery_candidate_id=discovery_candidate_id,
        source_candidate_id=uuid4(),
        canonical_url=f"https://core-{unique.hex}.example/report",
        role=SourceRole.PRIMARY,
        title="Core report",
        publisher="Test publisher",
        published_at=date(2026, 9, 15),
        tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
    )
    snapshot = ProductionInputSnapshot(
        production_run_id=run.id,
        edition_id=edition.id,
        subject_id=subject.id,
        subject_version=1,
        subject_title=subject.title,
        subject_tlp=TLP.CLEAR,
        selection_decision_id=decision.id,
        origin_discovery_subject_id=subject.id,
        canonical_discovery_subject_id=subject.id,
        discovery_snapshot_id=discovery_snapshot.id,
        discovery_snapshot_version=discovery_snapshot.version,
        member_candidate_ids=(discovery_candidate_id,),
        discovery_summary="The base campaign source is archived.",
        actor_or_campaign="Test actor",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        publication_language="en",
        research_date=research_date,
        core_sources=(core_source,),
    )
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        assert await uow.discovery_merge_runs.add_if_absent(merge_run)
        await uow.discovery_subject_identities.add_many_if_absent(
            [
                DiscoverySubjectIdentity(
                    edition_id=edition.id,
                    origin_key=f"subject:{subject.id}",
                    created_by_merge_run_id=merge_run.id,
                    id=subject.id,
                )
            ]
        )
        await uow.discovery_snapshots.append(discovery_snapshot)
        await uow.selection_decisions.append(decision)
        await uow.subject_discovery_origins.add(
            SubjectDiscoveryOrigin(
                subject_id=subject.id,
                edition_id=edition.id,
                discovery_subject_id=subject.id,
                selection_decision_id=decision.id,
                selected_snapshot_id=discovery_snapshot.id,
                selected_snapshot_version=discovery_snapshot.version,
            )
        )
        await uow.production_runs.add(run)
        await uow.production_input_snapshots.add(snapshot)
        await uow.commit()

    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    collection_service = SubjectCollectionService(
        uow_factory,
        SafeHttpCollector(_NoNetworkTransport(), _NoNetworkResolver()),
        blob_store,
    )
    core_collection = await collection_service.ensure_supplemental_source(
        subject.id,
        SupplementalSource(
            url=core_source.canonical_url,
            title=core_source.title,
            publisher=core_source.publisher,
            published_at=core_source.published_at,
            role=SourceRole.PRIMARY,
        ),
    )
    await collection_service.archive_manual_content(
        core_collection.id,
        content=b"# Base report\nThe incident affected a test organization.\n",
        declared_mime_type="text/markdown",
        actor_id="integration-analyst",
    )

    async with uow_factory() as uow:
        observations = await observe_reference_collections(
            uow, subject.id, (core_source.canonical_url,)
        )
        references = build_production_reference_corpus(
            subject_id=subject.id,
            research_date=research_date,
            production_input_hash=snapshot.input_hash,
            core_sources=snapshot.core_sources,
            proposals=(),
            observations=observations,
            warnings=(),
        )
    reference = references.sources[0]
    assert reference.source_document_id is not None
    assert reference.content_sha256 is not None
    references_hash = hashlib.sha256(
        artifact_store.canonical_json_bytes(production_reference_corpus_to_json(references))
    ).hexdigest()
    base_extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject.id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=references_hash,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=reference.source_document_id,
                canonical_url=reference.canonical_url,
                content_sha256=reference.content_sha256,
                tier=reference.tier,
                kind=reference.kind,
                role=reference.role,
                profile=ExtractionProfile.FULL,
                checkpoint_id=uuid4(),
                reuse_state=ExtractionReuseState.FRESH,
                facts=(),
                events=(),
                indicators=(),
                rules=(),
                uncertainties=(),
                editorial_role=ProductionEditorialRole.PRIMARY,
                profile_reason_code=ExtractionProfileReasonCode.CORE_PRIMARY_SOURCE,
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )
    base_projection = build_relevance_projection(snapshot, base_extraction)
    base_payloads = {
        ProductionArtifactStage.REFERENCES: production_reference_corpus_to_json(references),
        ProductionArtifactStage.EXTRACTION: production_extraction_to_json(base_extraction),
        ProductionArtifactStage.RELEVANCE_PROJECTION: relevance_projection_to_json(base_projection),
    }
    base_artifacts = []
    for stage, payload in base_payloads.items():
        _raw_id, blob_id, _rendered_id = await artifact_store.store_stage_payloads(
            canonical=payload
        )
        assert blob_id is not None
        base_artifacts.append(
            ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject.id,
                stage=stage,
                version=1,
                input_hash=hashlib.sha256(artifact_store.canonical_json_bytes(payload)).hexdigest(),
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=blob_id,
            )
        )
    async with uow_factory() as uow:
        for artifact in base_artifacts:
            await uow.production_artifacts.append(artifact)
        await uow.commit()

    description = (
        "NanoDump is an open-source tool for reading LSASS process memory and writing a minidump."
    )
    readme = (
        "# NanoDump\n\n"
        f"{description}\n"
        "The executable is nanodump.exe.\n"
        "The project website is nanodump.example.\n"
    ).encode()
    gateway = _StructuredGateway(
        {
            "NanoDump": Q2SourceOutput(
                facts=[
                    Q2FactProposal(
                        category="tools",
                        value=description,
                        evidence_quote=description,
                    )
                ],
                artifacts=[
                    Q2ArtifactProposal(
                        value="nanodump.exe",
                        artifact_type="filename",
                        indicator_status="contextual",
                        context="The source names nanodump.exe as its executable.",
                        evidence_quote="nanodump.exe",
                    ),
                    Q2ArtifactProposal(
                        value="nanodump.example",
                        artifact_type="domain",
                        indicator_status="contextual",
                        context="The source names the project website.",
                        evidence_quote="nanodump.example",
                    ),
                ],
            )
        }
    )
    extraction_service = ProductionExtractionService(
        uow_factory=uow_factory,
        model_gateway=gateway,
        artifact_store=artifact_store,
    )
    service = SourceAdditionService(
        uow_factory=uow_factory,
        collection_service=collection_service,
        extraction_service=extraction_service,
        artifact_store=artifact_store,
    )
    return SimpleNamespace(
        edition=edition,
        subject=subject,
        run=run,
        snapshot=snapshot,
        readme=readme,
        description=description,
        gateway=gateway,
        service=service,
        artifact_store=artifact_store,
    )


class _CrashAfterManualArchive:
    """Let the real archive commit, then simulate process loss before journaling."""

    def __init__(self, service: SubjectCollectionService) -> None:
        self.service = service
        self.archive_calls = 0

    @property
    def policy_snapshot(self):
        return self.service.policy_snapshot

    async def ensure_supplemental_source(self, *args: object, **kwargs: object):
        return await self.service.ensure_supplemental_source(*args, **kwargs)

    async def archive_one(self, *args: object, **kwargs: object):
        return await self.service.archive_one(*args, **kwargs)

    async def archive_manual_content(self, *args: object, **kwargs: object):
        await self.service.archive_manual_content(*args, **kwargs)
        self.archive_calls += 1
        raise RuntimeError("simulated crash after archive commit")


class _SimulatedAdditionExtraction:
    async def plan_for_corpus(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        corpus: ProductionReferenceCorpusV1,
        source_document_ids: frozenset[UUID],
        source_profile_intents: object | None = None,
    ):
        del source_profile_intents
        return SimpleNamespace(
            input_hash="f" * 64,
            run=run,
            snapshot=snapshot,
            corpus=corpus,
            source_document_ids=source_document_ids,
        )

    async def execute(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        plan: Any,
    ):
        del run
        corpus = plan.corpus
        sources = []
        for reference in corpus.sources:
            if reference.source_document_id not in plan.source_document_ids:
                continue
            assert reference.source_document_id is not None
            assert reference.content_sha256 is not None
            profile, _reason = extraction_profile_decision(
                tier=reference.tier,
                kind=reference.kind,
                editorial_role=reference.editorial_role,
            )
            sources.append(
                ProductionSourceExtractionV1(
                    source_document_id=reference.source_document_id,
                    canonical_url=reference.canonical_url,
                    content_sha256=reference.content_sha256,
                    tier=reference.tier,
                    kind=reference.kind,
                    role=reference.role,
                    profile=profile,
                    checkpoint_id=uuid4(),
                    reuse_state=ExtractionReuseState.FRESH,
                    facts=(),
                    events=(),
                    indicators=(),
                    rules=(),
                    uncertainties=("Simulated extraction from the archived document",),
                    editorial_role=reference.editorial_role,
                )
            )
        extraction = ProductionExtractionV1(
            schema_version=1,
            subject_id=snapshot.subject_id,
            production_input_hash=snapshot.input_hash,
            references_corpus_hash=hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(
                    production_reference_corpus_to_json(corpus)
                )
            ).hexdigest(),
            profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
            sources=tuple(sources),
            omitted_sources=(),
            warnings=(),
        )
        return SimpleNamespace(
            status=ExtractionExecutionStatus.SUCCEEDED,
            extraction=extraction,
            model_calls=0,
            error_code=None,
        )


@pytest.mark.asyncio
async def test_manual_addition_acquire_recovers_real_archive_once_and_reaches_ready(
    uow_factory: UnitOfWorkFactory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unique = uuid4()
    country_code = reserve_edition_code()
    edition = Edition(
        country=f"Additionland {country_code}",
        country_code=country_code,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        tlp=TLP.CLEAR,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Manual source addition integration",
        slug=f"manual-source-addition-{unique.hex}",
        tlp=TLP.CLEAR,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        research_date=date(2026, 10, 1),
    )
    snapshot = ProductionInputSnapshot(
        production_run_id=run.id,
        edition_id=edition.id,
        subject_id=subject.id,
        subject_version=1,
        subject_title=subject.title,
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=subject.id,
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Manual integration source addition",
        actor_or_campaign="Test actor",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        publication_language="en",
        research_date=run.research_date,
    )
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.commit()

    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    core_document_id = uuid4()
    core_collection_id = uuid4()
    core_url = "https://baseline.example/core"
    core_sha256 = "a" * 64
    core_reference = ProductionReferenceSourceV1(
        canonical_url=core_url,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        title="Baseline core source",
        publisher="Test publisher",
        published_at=None,
        source_collection_id=core_collection_id,
        source_document_id=core_document_id,
        discovery_candidate_ids=(),
        collection_state=CollectionState.ARCHIVED,
        content_sha256=core_sha256,
        relevance_reason=None,
        proposed_by_model=False,
        eligible_for_extraction=True,
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    base_references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject.id,
        research_date=run.research_date,
        production_input_hash=snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(core_reference,),
        warnings=(),
    )
    references_hash = hashlib.sha256(
        artifact_store.canonical_json_bytes(production_reference_corpus_to_json(base_references))
    ).hexdigest()
    base_extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject.id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=references_hash,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=core_document_id,
                canonical_url=core_url,
                content_sha256=core_sha256,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                profile=ExtractionProfile.FULL,
                checkpoint_id=uuid4(),
                reuse_state=ExtractionReuseState.FRESH,
                facts=(),
                events=(),
                indicators=(),
                rules=(),
                uncertainties=(),
                editorial_role=ProductionEditorialRole.PRIMARY,
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )
    base_projection = build_relevance_projection(snapshot, base_extraction)
    payloads = {
        ProductionArtifactStage.REFERENCES: production_reference_corpus_to_json(base_references),
        ProductionArtifactStage.EXTRACTION: production_extraction_to_json(base_extraction),
        ProductionArtifactStage.RELEVANCE_PROJECTION: relevance_projection_to_json(base_projection),
    }
    stored_base_artifacts = []
    for index, (stage, payload) in enumerate(payloads.items(), start=1):
        _raw_id, blob_id, _rendered_id = await artifact_store.store_stage_payloads(
            canonical=payload
        )
        assert blob_id is not None
        stored_base_artifacts.append(
            ProductionArtifact(
                production_run_id=run.id,
                subject_id=subject.id,
                stage=stage,
                version=1,
                input_hash=str(index) * 64,
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=blob_id,
            )
        )
    async with uow_factory() as uow:
        for artifact in stored_base_artifacts:
            await uow.production_artifacts.append(artifact)
        await uow.commit()

    collection_service = SubjectCollectionService(
        uow_factory,
        SafeHttpCollector(_NoNetworkTransport(), _NoNetworkResolver()),
        blob_store,
    )
    crashing_collection_service = _CrashAfterManualArchive(collection_service)
    service = SourceAdditionService(
        uow_factory=uow_factory,
        collection_service=crashing_collection_service,  # type: ignore[arg-type]
        extraction_service=_SimulatedAdditionExtraction(),  # type: ignore[arg-type]
        artifact_store=artifact_store,
    )
    artifacts_by_stage = {artifact.stage: artifact for artifact in stored_base_artifacts}
    context = SimpleNamespace(
        run=run,
        snapshot=snapshot,
        references_artifact=artifacts_by_stage[ProductionArtifactStage.REFERENCES],
        extraction_artifact=artifacts_by_stage[ProductionArtifactStage.EXTRACTION],
        projection_artifact=artifacts_by_stage[ProductionArtifactStage.RELEVANCE_PROJECTION],
        references=base_references,
        extraction=base_extraction,
        projection=base_projection,
        references_bytes=artifact_store.canonical_json_bytes(
            production_reference_corpus_to_json(base_references)
        ),
        extraction_bytes=artifact_store.canonical_json_bytes(
            production_extraction_to_json(base_extraction)
        ),
        projection_bytes=artifact_store.canonical_json_bytes(
            relevance_projection_to_json(base_projection)
        ),
    )

    async def load_test_context(_uow: object, _run_id: UUID):
        return context

    monkeypatch.setattr(service, "_load_context", load_test_context)

    async def checkpoint_for_source(*_args: object):
        return SimpleNamespace(
            model_run_id=uuid4(),
            contract_version="test-contract",
            prompt_version="test-prompt",
            parser_version="test-parser",
            verifier_version="test-verifier",
            source_text_contract_version="test-source-text",
            model_policy_version="test-model-policy",
            routing_policy_version="test-routing-policy",
        )

    monkeypatch.setattr(service, "_checkpoint_for_source", checkpoint_for_source)
    request_id = uuid4()
    url = f"https://manual-{unique.hex}.example/source"
    body = b"# Archived manual source\nA stable integration fixture.\n"
    await service.record_candidate(
        production_run_id=run.id,
        request_id=request_id,
        url=url,
        origin=SourceAdditionOrigin.MANUAL_ARCHIVE,
        title="Manual source",
        publisher="Test publisher",
    )
    await service.select(
        production_run_id=run.id,
        request_id=request_id,
        url=url,
        criterion="Adds a bounded source for the test",
        actor_id="integration-analyst",
    )

    with pytest.raises(RuntimeError, match="simulated crash after archive commit"):
        await service.acquire(
            production_run_id=run.id,
            request_id=request_id,
            url=url,
            manual_content=body,
            declared_mime_type="text/markdown",
        )

    acquired = await service.acquire(
        production_run_id=run.id,
        request_id=request_id,
        url=url,
        manual_content=body,
        declared_mime_type="text/markdown",
    )
    assert acquired.refusal is None
    assert acquired.addition.state is SourceAdditionState.READY
    assert acquired.addition.checkpoint_id is not None
    assert acquired.addition.evidence_handles
    assert crashing_collection_service.archive_calls == 1

    resumed = await service.acquire(
        production_run_id=run.id,
        request_id=request_id,
        url=url,
    )
    assert resumed.addition == acquired.addition
    assert crashing_collection_service.archive_calls == 1

    async with uow_factory() as uow:
        collection = await uow.source_collections.get(acquired.addition.collection_id)
        assert collection is not None
        assert collection.origin_kind is SourceOriginKind.MANUAL
        assert collection.fetch_policy_snapshot_id is None
        attempts = await uow.collection_attempts.list_for_collection(collection.id)
        assert len(attempts) == 1
        assert attempts[0].policy_snapshot_id == acquired.addition.collection_policy_snapshot_id
        latest = await uow.production_source_additions.get_latest_for_identity(
            run.id, request_id, source_addition_source_key(url)
        )
        assert latest is not None
        reference_projection = next(
            item
            for item in latest.promotion_plan.projected_artifacts
            if item.stage is ProductionArtifactStage.REFERENCES
        )

    projected_corpus = production_reference_corpus_from_json(
        await artifact_store.read_json(reference_projection.canonical_blob_id)
    )
    async with uow_factory() as uow:
        hash_divergent = replace(acquired.addition, decoded_sha256="d" * 64)
        refused_hash = await service._archived_receipt_refusal(
            uow, run, projected_corpus, hash_divergent
        )
        divergent_reference = replace(
            next(
                item
                for item in projected_corpus.sources
                if item.canonical_url == acquired.addition.canonical_url
            ),
            source_document_id=uuid4(),
        )
        document_divergent_corpus = replace(
            projected_corpus,
            sources=tuple(
                divergent_reference
                if item.canonical_url == acquired.addition.canonical_url
                else item
                for item in projected_corpus.sources
            ),
        )
        refused_document = await service._archived_receipt_refusal(
            uow, run, document_divergent_corpus, acquired.addition
        )
    assert refused_hash is not None
    assert refused_hash.code is SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE
    assert refused_document is not None
    assert refused_document.code is SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE


@pytest.mark.asyncio
async def test_descriptive_addition_uses_full_checkpoint_and_stays_contextual(
    uow_factory: UnitOfWorkFactory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _seed_real_source_addition_service(
        uow_factory=uow_factory,
        tmp_path=tmp_path,
    )
    full_request_id = uuid4()
    full_url = f"https://nanodump-full-{uuid4().hex}.example/readme"
    await world.service.record_candidate(
        production_run_id=world.run.id,
        request_id=full_request_id,
        url=full_url,
        origin=SourceAdditionOrigin.MANUAL_ARCHIVE,
        title="NanoDump README",
        publisher="Fortra",
        extraction_intent=SourceAdditionExtractionIntent.DESCRIPTIVE_FACTS,
    )
    await world.service.select(
        production_run_id=world.run.id,
        request_id=full_request_id,
        url=full_url,
        criterion="Explain the tool described in the requested section",
        actor_id="integration-analyst",
    )

    save_transition = world.service._save_transition
    should_crash = True

    async def crash_after_checkpoint(**kwargs: Any):
        nonlocal should_crash
        if kwargs["addition"].state is SourceAdditionState.EXTRACTED and should_crash:
            should_crash = False
            raise RuntimeError("simulated process loss after checkpoint")
        return await save_transition(**kwargs)

    monkeypatch.setattr(world.service, "_save_transition", crash_after_checkpoint)
    with pytest.raises(RuntimeError, match="after checkpoint"):
        await world.service.acquire(
            production_run_id=world.run.id,
            request_id=full_request_id,
            url=full_url,
            manual_content=world.readme,
            declared_mime_type="text/markdown",
        )
    assert len(world.gateway.calls) == 1

    full_result = await world.service.acquire(
        production_run_id=world.run.id,
        request_id=full_request_id,
        url=full_url,
    )
    assert full_result.refusal is None
    full_intent = full_result.addition.extraction_intent
    assert full_intent is SourceAdditionExtractionIntent.DESCRIPTIVE_FACTS
    assert full_result.addition.extraction_profile is ExtractionProfile.FULL
    assert (
        full_result.addition.extraction_profile_policy_version
        == SOURCE_ADDITION_EXTRACTION_PROFILE_POLICY_VERSION
    )
    assert (
        full_result.addition.profile_reason_code
        is ExtractionProfileReasonCode.OPERATOR_DESCRIPTIVE_CONTEXT
    )
    assert full_result.addition.reuse_state is ExtractionReuseState.REUSED
    assert full_result.model_calls == 0
    assert any(handle.kind is EvidenceKind.FACT for handle in full_result.addition.evidence_handles)
    assert len(world.gateway.calls) == 1

    full_preview = await world.service.preview(
        production_run_id=world.run.id,
        request_id=full_request_id,
    )
    assert full_preview.ready
    full_manifest = full_preview.source_additions[0]
    assert full_manifest["extraction_intent"] == "descriptive_facts"
    assert full_manifest["extraction_profile"] == ExtractionProfile.FULL.value
    assert full_manifest["profile_reason_code"] == "operator_descriptive_context"
    assert (
        full_manifest["extraction_profile_policy_version"]
        == SOURCE_ADDITION_EXTRACTION_PROFILE_POLICY_VERSION
    )
    assert full_preview.model_calls["addition_extraction"] == {
        "made": 0,
        "remaining": 0,
        "checkpoint_reuses": 1,
    }

    async with uow_factory() as uow:
        full_entry = await uow.production_source_additions.get_latest_for_identity(
            world.run.id,
            full_request_id,
            source_addition_source_key(full_url),
        )
    assert full_entry is not None and full_entry.promotion_plan is not None
    projected_extraction = next(
        item
        for item in full_entry.promotion_plan.projected_artifacts
        if item.stage is ProductionArtifactStage.EXTRACTION
    )
    projected_relevance = next(
        item
        for item in full_entry.promotion_plan.projected_artifacts
        if item.stage is ProductionArtifactStage.RELEVANCE_PROJECTION
    )
    extraction = production_extraction_from_json(
        await world.artifact_store.read_json(projected_extraction.canonical_blob_id)
    )
    projection = relevance_projection_from_json(
        await world.artifact_store.read_json(projected_relevance.canonical_blob_id)
    )
    full_source_id = full_result.addition.source_document_id
    assert full_source_id is not None
    fact_ref = next(
        ref
        for ref in extraction_evidence_refs_v1(extraction)
        if ref.source_document_id == full_source_id and ref.kind is EvidenceKind.FACT
    )
    full_source_extraction = next(
        source for source in extraction.sources if source.source_document_id == full_source_id
    )
    assert full_source_extraction.editorial_role is ProductionEditorialRole.CONTEXT
    assert projection.classification_for(fact_ref).classification is RelevanceClassification.CONTEXT

    evidence_pack = build_synthesis_evidence_pack(world.snapshot, extraction, projection)
    context_handle = evidence_pack.handle_for(fact_ref)
    assert context_handle is not None
    descriptive_claim = SynthesisClaimProposalV1(
        text=world.description,
        evidence_handles=(context_handle,),
        claim_scope="tool_context",
    )
    tool_context_proposal = SynthesisProposalV1(
        lead=(),
        sections=(
            SynthesisSectionProposalV1(
                kind=SynthesisSectionKind.OVERVIEW,
                heading="",
                claims=(descriptive_claim,),
            ),
        ),
    )
    _assert_claim_handles_and_relations_supported(
        tool_context_proposal,
        evidence_pack,
        extraction=extraction,
        source_addition_document_ids=frozenset({full_source_id}),
        require_claim_scopes=True,
    )
    actor_activity_proposal = SynthesisProposalV1(
        lead=(),
        sections=(
            SynthesisSectionProposalV1(
                kind=SynthesisSectionKind.OVERVIEW,
                heading="",
                claims=(
                    SynthesisClaimProposalV1(
                        text=world.description,
                        evidence_handles=(context_handle,),
                        claim_scope="actor_activity",
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(
        TargetedSynthesisRevisionValidationError,
        match="revision_tool_context_cannot_support_actor_activity",
    ):
        _assert_claim_handles_and_relations_supported(
            actor_activity_proposal,
            evidence_pack,
            extraction=extraction,
            source_addition_document_ids=frozenset({full_source_id}),
            require_claim_scopes=True,
        )

    indicators_request_id = uuid4()
    indicators_url = f"https://nanodump-ioc-{uuid4().hex}.example/readme"
    await world.service.record_candidate(
        production_run_id=world.run.id,
        request_id=indicators_request_id,
        url=indicators_url,
        origin=SourceAdditionOrigin.MANUAL_ARCHIVE,
        title="NanoDump README",
        publisher="Fortra",
    )
    await world.service.select(
        production_run_id=world.run.id,
        request_id=indicators_request_id,
        url=indicators_url,
        criterion="Keep the default indicator-only extraction",
        actor_id="integration-analyst",
    )
    indicators_result = await world.service.acquire(
        production_run_id=world.run.id,
        request_id=indicators_request_id,
        url=indicators_url,
        manual_content=world.readme,
        declared_mime_type="text/markdown",
    )
    assert indicators_result.refusal is None
    assert (
        indicators_result.addition.extraction_intent
        is SourceAdditionExtractionIntent.INDICATORS_ONLY
    )
    assert indicators_result.addition.extraction_profile is ExtractionProfile.IOC_RULES
    assert (
        indicators_result.addition.profile_reason_code
        is ExtractionProfileReasonCode.TECHNICAL_ANNEX
    )
    assert not any(
        handle.kind is EvidenceKind.FACT for handle in indicators_result.addition.evidence_handles
    )
    assert any(
        handle.kind is EvidenceKind.INDICATOR
        for handle in indicators_result.addition.evidence_handles
    )
    assert indicators_result.model_calls == 1
    assert len(world.gateway.calls) == 2

    indicators_preview = await world.service.preview(
        production_run_id=world.run.id,
        request_id=indicators_request_id,
    )
    indicators_manifest = indicators_preview.source_additions[0]
    assert indicators_manifest["extraction_intent"] == "indicators_only"
    assert indicators_manifest["extraction_profile"] == ExtractionProfile.IOC_RULES.value
    assert indicators_preview.model_calls["addition_extraction"] == {
        "made": 1,
        "remaining": 0,
        "checkpoint_reuses": 0,
    }

    async with uow_factory() as uow:
        full_checkpoint = next(
            row
            for row in await uow.source_extractions.list_for_url(full_url)
            if row.id == full_result.addition.checkpoint_id
        )
        indicators_checkpoint = next(
            row
            for row in await uow.source_extractions.list_for_url(indicators_url)
            if row.id == indicators_result.addition.checkpoint_id
        )
    assert full_result.addition.decoded_sha256 == indicators_result.addition.decoded_sha256
    assert full_checkpoint.source_content_sha256 == indicators_checkpoint.source_content_sha256
    assert full_checkpoint.profile is not indicators_checkpoint.profile
    assert full_checkpoint.id != indicators_checkpoint.id
    assert full_checkpoint.profile_policy_version != indicators_checkpoint.profile_policy_version

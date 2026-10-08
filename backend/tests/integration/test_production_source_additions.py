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
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.http_collection import (
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import ExtractionExecutionStatus
from cti_app.application.production_references import (
    production_reference_corpus_from_json,
    production_reference_corpus_to_json,
)
from cti_app.application.production_relevance import (
    build_relevance_projection,
    relevance_projection_to_json,
)
from cti_app.application.production_source_additions import (
    SourceAdditionRefusalCode,
    SourceAdditionService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_extraction import (
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    extraction_profile_decision,
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
from cti_app.domain.production_source_additions import (
    SourceAdditionJournalEntryV1,
    SourceAdditionOrigin,
    SourceAdditionState,
    SourceAdditionV1,
    source_addition_source_key,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_source_addition_repository_round_trips_immutable_versions(
    uow_factory: UnitOfWorkFactory,
) -> None:
    edition = Edition(
        country="France",
        country_code="FR",
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
        await uow.editions.add_if_absent(edition)
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
    edition = Edition(
        country="France",
        country_code="FR",
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
        await uow.editions.add_if_absent(edition)
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
    ):
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
    country_code = "".join(chr(ord("A") + ((unique.int >> shift) % 26)) for shift in (0, 8))
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

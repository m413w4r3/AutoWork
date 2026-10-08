"""Acquisition and prospective projection tests for additive sources."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import (
    ExtractionExecutionStatus,
    ProductionExtractionService,
)
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_relevance import (
    build_relevance_projection,
    relevance_projection_to_json,
)
from cti_app.application.production_source_additions import (
    SourceAdditionConflictError,
    SourceAdditionPromotionBlockedError,
    SourceAdditionRefusalCode,
    SourceAdditionService,
    _merge_additive_extraction,
    _rebase_relevance_projection,
)
from cti_app.application.production_synthesis import (
    extraction_evidence_refs_v1,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    CollectionFailureReason,
    CollectionState,
    SourceCollection,
    SourceOriginKind,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
    SourceAdditionExtractionIntent,
)
from cti_app.domain.production_extraction import (
    ExtractionProfileReasonCode,
    ExtractionReuseState,
    ProductionExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
    ProductionSourceCriticality,
    ProductionSourceCriticalityReason,
)
from cti_app.domain.production_source_additions import (
    SourceAdditionArtifactFenceV1,
    SourceAdditionArtifactProjectionV1,
    SourceAdditionJournalEntryV1,
    SourceAdditionOrigin,
    SourceAdditionPromotionPlanV1,
    SourceAdditionState,
    SourceAdditionV1,
    source_addition_from_json,
    source_addition_manifest,
    source_addition_source_key,
    source_addition_to_json,
    source_additions_hash,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from tests.test_production_extraction_service import (
    SUPPORT_TEXT,
    _core_world,
    _corpus,
    _full_output,
    _reference,
    _register_source,
    _StructuredGateway,
    _support_output,
)
from tests.test_production_extraction_service import (
    _snapshot as _extraction_snapshot,
)
from tests.test_production_relevance_domain import _fact, _source
from tests.test_production_relevance_domain import _snapshot as _relevance_snapshot


class _Context:
    def __init__(self, uow: object) -> None:
        self._uow = uow

    async def __aenter__(self) -> object:
        return self._uow

    async def __aexit__(self, *args: object) -> None:
        del args


class _Factory:
    def __init__(self, uow: object) -> None:
        self.uow = uow

    def __call__(self) -> _Context:
        return _Context(self.uow)


class _AdditionRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, UUID, str], list[SourceAdditionJournalEntryV1]] = {}

    async def get_latest_for_identity(
        self, run_id: UUID, request_id: UUID, source_key: str
    ) -> SourceAdditionJournalEntryV1 | None:
        entries = self.rows.get((run_id, request_id, source_key), [])
        return entries[-1] if entries else None

    async def list_latest_for_request(
        self, run_id: UUID, request_id: UUID
    ) -> tuple[SourceAdditionJournalEntryV1, ...]:
        return tuple(
            entries[-1]
            for (row_run_id, row_request_id, _), entries in self.rows.items()
            if row_run_id == run_id and row_request_id == request_id
        )

    async def append(self, entry: SourceAdditionJournalEntryV1) -> None:
        addition = entry.addition
        identity = (
            addition.production_run_id,
            addition.request_id,
            source_addition_source_key(addition.canonical_url),
        )
        self.rows.setdefault(identity, []).append(entry)


class _RunRepository:
    def __init__(self, run: ProductionRun) -> None:
        self.run = run

    async def get(self, _run_id: UUID) -> ProductionRun:
        return self.run

    async def get_for_update(self, _run_id: UUID) -> ProductionRun:
        return self.run


class _JournalUow:
    def __init__(self, run: ProductionRun) -> None:
        self.production_runs = _RunRepository(run)
        self.production_source_additions = _AdditionRepository()
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _ManualCollectionService:
    def __init__(self, collection: SourceCollection, document: object, body: bytes) -> None:
        self.collection = collection
        self.document = document
        self.body = body
        self.manual_calls: list[tuple[bytes, str, str]] = []
        self.fetch_calls = 0
        self.attempt = SimpleNamespace(
            id=uuid4(),
            collection_id=collection.id,
            manual_lease_id=uuid4(),
            job_id=None,
            policy_snapshot_id="policy-v1",
            encoded_sha256=hashlib.sha256(body).hexdigest(),
            decoded_sha256=hashlib.sha256(body).hexdigest(),
        )

    async def ensure_supplemental_source(self, _subject_id: UUID, _source: object):
        return self.collection

    async def archive_one(self, _collection_id: UUID, _job_id: UUID) -> CollectionState:
        self.fetch_calls += 1
        return CollectionState.BLOCKED

    async def archive_manual_content(
        self,
        _collection_id: UUID,
        *,
        content: bytes,
        declared_mime_type: str,
        final_url: str,
        actor_id: str,
        return_receipt: bool,
    ):
        assert return_receipt is True
        self.manual_calls.append((content, declared_mime_type, actor_id))
        self.collection.state = CollectionState.ARCHIVED
        self.collection.failure_reason_code = None
        self.collection.source_document_id = self.document.id
        self.collection.origin_kind = SourceOriginKind.MANUAL
        self.collection.fetch_policy_snapshot_id = None
        self.collection.latest_attempt_id = self.attempt.id
        receipt = SimpleNamespace(
            encoded_sha256=hashlib.sha256(content).hexdigest(),
            decoded_sha256=hashlib.sha256(content).hexdigest(),
        )
        return self.collection, receipt

    def policy_snapshot(self) -> object:
        return SimpleNamespace(id="policy-v1")


class _ArchiveUow:
    def __init__(
        self,
        run: ProductionRun,
        collection: SourceCollection,
        document: object,
        attempt: object | None = None,
    ) -> None:
        self.production_runs = _RunRepository(run)
        self.production_artifacts = SimpleNamespace(get_current=self._no_artifact)
        self.source_collections = SimpleNamespace(get=self._collection)
        self.source_documents = SimpleNamespace(get=self._document)
        self.collection_attempts = SimpleNamespace(
            list_for_collection=lambda _collection_id: _async_value(
                (attempt,) if attempt is not None else ()
            )
        )
        self._collection_row = collection
        self._document_row = document

    async def _no_artifact(self, _run_id: UUID, _stage: str):
        return None

    async def _collection(self, _collection_id: UUID):
        return self._collection_row

    async def _document(self, _document_id: UUID):
        return self._document_row


def _service(
    uow_factory: object,
    collection_service: object | None = None,
    artifact_store: object | None = None,
) -> Any:
    return SourceAdditionService(
        uow_factory=uow_factory,  # type: ignore[arg-type]
        collection_service=collection_service,  # type: ignore[arg-type]
        extraction_service=None,  # type: ignore[arg-type]
        artifact_store=artifact_store,  # type: ignore[arg-type]
    )


def _ready_addition(
    *,
    run_id: UUID,
    subject_id: UUID,
    request_id: UUID,
    url: str = "https://example.test/added",
) -> SourceAdditionV1:
    document_id = uuid4()
    return SourceAdditionV1(
        addition_id=uuid4(),
        request_id=request_id,
        production_run_id=run_id,
        subject_id=subject_id,
        state=SourceAdditionState.READY,
        origin=SourceAdditionOrigin.OPERATOR_URL,
        canonical_url=url,
        final_url=url,
        title="Supplemental source",
        publisher="Example publisher",
        collection_id=uuid4(),
        source_document_id=document_id,
        encoded_sha256="a" * 64,
        decoded_sha256="b" * 64,
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
        role=SourceRole.UNKNOWN,
        editorial_role=ProductionEditorialRole.CONTEXT,
        criticality=ProductionSourceCriticality.CRITICAL,
        criticality_reason=ProductionSourceCriticalityReason.COVERAGE_UNKNOWN,
        selection_criterion="Adds technical context to the case",
        selection_criterion_version="criterion-v1",
        extraction_profile=ExtractionProfile.IOC_RULES,
        profile_reason_code=ExtractionProfileReasonCode.TECHNICAL_ANNEX,
        reuse_state=ExtractionReuseState.FRESH,
        extraction_profile_policy_version="profile-v1",
        checkpoint_contract_version="contract-v1",
        checkpoint_prompt_version="prompt-v1",
        checkpoint_parser_version="parser-v1",
        checkpoint_verifier_version="verifier-v1",
        source_text_contract_version="text-v1",
        model_policy_version="model-v1",
        routing_policy_version="routing-v1",
        collection_policy_snapshot_id="policy-v1",
        selected_by="analyst@example.test",
        checkpoint_id=uuid4(),
        extraction_id=uuid4(),
        model_run_id=uuid4(),
        evidence_handles=(
            ExtractionEvidenceRefV1(
                source_document_id=document_id,
                kind=EvidenceKind.FACT,
                evidence_key="c" * 64,
            ),
        ),
    )


def _promotion_plan(
    addition: SourceAdditionV1,
    run: ProductionRun,
    base_artifacts: tuple[ProductionArtifact, ...] | None = None,
) -> SourceAdditionPromotionPlanV1:
    stages = (
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.RELEVANCE_PROJECTION,
    )
    return SourceAdditionPromotionPlanV1(
        production_run_id=run.id,
        subject_id=run.subject_id,
        pipeline_generation=run.pipeline_generation,
        base_artifacts=(
            tuple(
                SourceAdditionArtifactFenceV1(
                    stage=stage,
                    artifact_id=artifact.id,
                    version=artifact.version,
                    input_hash=artifact.input_hash,
                    canonical_sha256=hashlib.sha256(
                        f"base:{artifact.id}".encode("ascii")
                    ).hexdigest(),
                )
                for stage, artifact in zip(stages, base_artifacts, strict=True)
            )
            if base_artifacts is not None
            else tuple(
                SourceAdditionArtifactFenceV1(
                    stage=stage,
                    artifact_id=uuid4(),
                    version=1,
                    input_hash="d" * 64,
                    canonical_sha256="e" * 64,
                )
                for stage in stages
            )
        ),
        projected_artifacts=tuple(
            SourceAdditionArtifactProjectionV1(
                stage=stage,
                artifact_id=uuid4(),
                version=2,
                input_hash="f" * 64,
                canonical_blob_id=uuid4(),
                canonical_sha256="0" * 64,
            )
            for stage in stages
        ),
        source_additions_hash=source_additions_hash((addition,)),
    )


def test_intent_url_is_canonical_and_selection_replay_is_idempotent() -> None:
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    uow = _JournalUow(run)
    service = _service(_Factory(uow))
    request_id = uuid4()

    async def exercise() -> None:
        candidate = await service.record_candidate(
            production_run_id=run.id,
            request_id=request_id,
            url="HTTPS://Example.test:443/report#section",
            origin=SourceAdditionOrigin.OPERATOR_URL,
        )
        replay = await service.record_candidate(
            production_run_id=run.id,
            request_id=request_id,
            url="https://example.test/report",
            origin=SourceAdditionOrigin.OPERATOR_URL,
        )
        selected = await service.select(
            production_run_id=run.id,
            request_id=request_id,
            url="https://example.test/report",
            criterion="Adds current-case evidence",
            actor_id="analyst@example.test",
        )
        selected_replay = await service.select(
            production_run_id=run.id,
            request_id=request_id,
            url="https://example.test/report",
            criterion="Adds current-case evidence",
            actor_id="analyst@example.test",
        )
        assert candidate.canonical_url == "https://example.test/report"
        assert replay == candidate
        assert selected.state is SourceAdditionState.SELECTED
        assert selected_replay == selected

    asyncio.run(exercise())
    entries = next(iter(uow.production_source_additions.rows.values()))
    assert [entry.version for entry in entries] == [1, 2]


def test_bounded_editorial_resource_proposal_becomes_candidate_not_evidence() -> None:
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    uow = _JournalUow(run)
    service = _service(_Factory(uow))
    request_id = uuid4()
    proposal = SimpleNamespace(
        url="HTTPS://Example.test/resource#proposal",
        justification="A bounded proposal for additional technical context",
        source_model_run_id=uuid4(),
        policy_version="editorial-resource-proposal-v1",
    )

    async def exercise() -> SourceAdditionV1:
        additions = await service.record_discovery_proposals(
            production_run_id=run.id,
            request_id=request_id,
            proposals=(proposal,),
        )
        candidate = additions[0]
        assert isinstance(candidate, SourceAdditionV1)
        return candidate

    candidate = asyncio.run(exercise())
    assert candidate.origin is SourceAdditionOrigin.DISCOVERY
    assert candidate.state is SourceAdditionState.CANDIDATE
    assert candidate.canonical_url == "https://example.test/resource"
    assert candidate.evidence_handles == ()
    assert candidate.model_run_id is None


@pytest.mark.asyncio
async def test_403_uses_only_operator_bytes_and_recovery_does_not_archive_twice() -> None:
    subject_id = uuid4()
    run = ProductionRun(
        subject_id=subject_id,
        edition_id=uuid4(),
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        research_date=date(2026, 8, 1),
    )
    body = b"<html><body>Operator archived source</body></html>"
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=run.edition_id,
        requested_url="https://example.test/added",
        canonical_url="https://example.test/added",
        proposed_role=SourceRole.UNKNOWN,
        origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
        state=CollectionState.BLOCKED,
        failure_reason_code=CollectionFailureReason.ACCESS_BLOCKED,
        source_tlp=TLP.AMBER,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    document = SimpleNamespace(
        id=uuid4(),
        subject_id=subject_id,
        final_url="https://example.test/final",
        title="Manual title",
        publisher="Manual publisher",
        published_at=date(2026, 7, 31),
        tlp=TLP.AMBER,
        do_not_submit=False,
        external_llm_allowed=True,
        encoded_sha256=hashlib.sha256(body).hexdigest(),
        decoded_sha256=hashlib.sha256(body).hexdigest(),
    )
    collection_service = _ManualCollectionService(collection, document, body)
    uow = _ArchiveUow(run, collection, document, collection_service.attempt)
    service = _service(_Factory(uow), collection_service)
    selected = replace(
        _ready_addition(run_id=run.id, subject_id=subject_id, request_id=uuid4()),
        state=SourceAdditionState.SELECTED,
        final_url=None,
        collection_id=None,
        source_document_id=None,
        encoded_sha256=None,
        decoded_sha256=None,
        collection_policy_snapshot_id=None,
        criticality=None,
        criticality_reason=None,
        extraction_profile=None,
        profile_reason_code=None,
        reuse_state=None,
        extraction_profile_policy_version=None,
        checkpoint_contract_version=None,
        checkpoint_prompt_version=None,
        checkpoint_parser_version=None,
        checkpoint_verifier_version=None,
        source_text_contract_version=None,
        model_policy_version=None,
        routing_policy_version=None,
        checkpoint_id=None,
        extraction_id=None,
        model_run_id=None,
        evidence_handles=(),
        selection_criterion="Adds current-case evidence",
        selection_criterion_version="criterion-v1",
        selected_by="analyst@example.test",
    )

    archived, refusal = await service._archive_selected(
        selected,
        job_id=uuid4(),
        manual_content=body,
        declared_mime_type="text/html",
        final_url="HTTPS://EXAMPLE.TEST:443/final#article",
        research_date=run.research_date,
    )
    assert refusal is None
    assert archived is not None
    assert archived.state is SourceAdditionState.ARCHIVED
    assert archived.canonical_url == "https://example.test/added"
    assert archived.final_url == "https://example.test/final"
    assert archived.collection_policy_snapshot_id == "policy-v1"
    assert collection.fetch_policy_snapshot_id is None
    assert collection_service.manual_calls == [(body, "text/html", "analyst@example.test")]

    resumed, resumed_refusal = await service._archive_selected(
        selected,
        job_id=uuid4(),
        manual_content=body,
        declared_mime_type="text/html",
        final_url=None,
        research_date=run.research_date,
    )
    assert resumed_refusal is None
    assert resumed == archived
    assert len(collection_service.manual_calls) == 1


@pytest.mark.asyncio
async def test_archived_resume_rejects_a_different_document_or_hash_for_same_url() -> None:
    subject_id, request_id = uuid4(), uuid4()
    run = ProductionRun(
        subject_id=subject_id,
        edition_id=uuid4(),
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        research_date=date(2026, 8, 1),
    )
    addition = _ready_addition(run_id=run.id, subject_id=subject_id, request_id=request_id)
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=run.edition_id,
        requested_url=addition.canonical_url,
        canonical_url=addition.canonical_url,
        proposed_role=SourceRole.UNKNOWN,
        origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
        id=addition.collection_id,
        state=CollectionState.ARCHIVED,
        source_document_id=addition.source_document_id,
        fetch_policy_snapshot_id=addition.collection_policy_snapshot_id,
        source_tlp=TLP.AMBER,
    )
    document = SimpleNamespace(
        id=addition.source_document_id,
        subject_id=subject_id,
        source_collection_id=addition.collection_id,
        encoded_sha256=addition.encoded_sha256,
        decoded_sha256=addition.decoded_sha256,
        final_url=addition.final_url,
        published_at=None,
        do_not_submit=False,
        external_llm_allowed=True,
        tlp=TLP.AMBER,
    )
    reference = ProductionReferenceSourceV1(
        canonical_url=addition.canonical_url,
        tier=addition.tier,
        kind=addition.kind,
        role=addition.role,
        title=addition.title,
        publisher=addition.publisher,
        published_at=None,
        source_collection_id=addition.collection_id,
        source_document_id=addition.source_document_id,
        discovery_candidate_ids=(),
        collection_state=CollectionState.ARCHIVED,
        content_sha256=addition.decoded_sha256,
        relevance_reason=addition.selection_criterion,
        proposed_by_model=False,
        eligible_for_extraction=True,
        editorial_role=ProductionEditorialRole.CONTEXT,
    )
    uow = SimpleNamespace(
        source_collections=SimpleNamespace(get=lambda _id: _async_value(collection)),
        source_documents=SimpleNamespace(get=lambda _id: _async_value(document)),
    )
    service = _service(_Factory(_JournalUow(run)))

    assert (
        await service._archived_receipt_refusal(
            uow, run, SimpleNamespace(sources=(reference,)), addition
        )
        is None
    )

    collection.fetch_policy_snapshot_id = None
    refusal = await service._archived_receipt_refusal(
        uow, run, SimpleNamespace(sources=(reference,)), addition
    )
    assert refusal is not None
    assert refusal.code is SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE
    collection.fetch_policy_snapshot_id = addition.collection_policy_snapshot_id

    document.decoded_sha256 = "d" * 64
    refusal = await service._archived_receipt_refusal(
        uow, run, SimpleNamespace(sources=(reference,)), addition
    )
    assert refusal is not None
    assert refusal.code is SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE


@pytest.mark.asyncio
async def test_manual_archive_resume_uses_its_persisted_attempt_policy_snapshot() -> None:
    subject_id, request_id = uuid4(), uuid4()
    run = ProductionRun(
        subject_id=subject_id,
        edition_id=uuid4(),
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        research_date=date(2026, 8, 1),
    )
    addition = _ready_addition(run_id=run.id, subject_id=subject_id, request_id=request_id)
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=run.edition_id,
        requested_url=addition.canonical_url,
        canonical_url=addition.canonical_url,
        proposed_role=SourceRole.UNKNOWN,
        origin_kind=SourceOriginKind.MANUAL,
        id=addition.collection_id,
        state=CollectionState.ARCHIVED,
        source_document_id=addition.source_document_id,
        fetch_policy_snapshot_id=None,
        latest_attempt_id=uuid4(),
        source_tlp=TLP.AMBER,
    )
    document = SimpleNamespace(
        id=addition.source_document_id,
        subject_id=subject_id,
        source_collection_id=addition.collection_id,
        encoded_sha256=addition.encoded_sha256,
        decoded_sha256=addition.decoded_sha256,
        final_url=addition.final_url,
        published_at=None,
        do_not_submit=False,
        external_llm_allowed=True,
        tlp=TLP.AMBER,
    )
    attempt = SimpleNamespace(
        id=collection.latest_attempt_id,
        collection_id=collection.id,
        manual_lease_id=uuid4(),
        job_id=None,
        policy_snapshot_id=addition.collection_policy_snapshot_id,
        encoded_sha256=addition.encoded_sha256,
        decoded_sha256=addition.decoded_sha256,
    )
    reference = ProductionReferenceSourceV1(
        canonical_url=addition.canonical_url,
        tier=addition.tier,
        kind=addition.kind,
        role=addition.role,
        title=addition.title,
        publisher=addition.publisher,
        published_at=None,
        source_collection_id=addition.collection_id,
        source_document_id=addition.source_document_id,
        discovery_candidate_ids=(),
        collection_state=CollectionState.ARCHIVED,
        content_sha256=addition.decoded_sha256,
        relevance_reason=addition.selection_criterion,
        proposed_by_model=False,
        eligible_for_extraction=True,
        editorial_role=ProductionEditorialRole.CONTEXT,
    )
    uow = SimpleNamespace(
        source_collections=SimpleNamespace(get=lambda _id: _async_value(collection)),
        source_documents=SimpleNamespace(get=lambda _id: _async_value(document)),
        collection_attempts=SimpleNamespace(
            list_for_collection=lambda _id: _async_value((attempt,))
        ),
    )
    service = _service(_Factory(_JournalUow(run)))

    assert (
        await service._archived_receipt_refusal(
            uow, run, SimpleNamespace(sources=(reference,)), addition
        )
        is None
    )

    attempt.policy_snapshot_id = "different-policy"
    refusal = await service._archived_receipt_refusal(
        uow, run, SimpleNamespace(sources=(reference,)), addition
    )
    assert refusal is not None
    assert refusal.code is SourceAdditionRefusalCode.ARCHIVE_RECEIPT_INCOMPLETE


@pytest.mark.asyncio
async def test_addition_extraction_runs_only_new_document_and_reuses_its_checkpoint() -> None:
    world, snapshot, base_corpus = _core_world()
    gateway = _StructuredGateway(
        {"ExampleRAT": _full_output(), "loader.security-lab.io": _support_output()}
    )
    service = ProductionExtractionService(
        uow_factory=world.uow,
        model_gateway=gateway,
        artifact_store=world.blobs,
    )
    run = ProductionRun(
        subject_id=snapshot.subject_id,
        edition_id=snapshot.edition_id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    old_plan = await service.plan_for_corpus(run=run, snapshot=snapshot, corpus=base_corpus)
    old_execution = await service.execute(run=run, snapshot=snapshot, plan=old_plan)
    assert old_execution.extraction is not None

    added_document_id, added_sha = _register_source(
        world,
        subject_id=snapshot.subject_id,
        url="https://example.test/support",
        text=SUPPORT_TEXT,
    )
    addition = _reference(
        url="https://example.test/support",
        tier=ProductionReferenceTier.SUPPORTING,
        document_id=added_document_id,
        sha256=added_sha,
        role=SourceRole.UNKNOWN,
    )
    prospective_corpus = _corpus(
        subject_id=snapshot.subject_id,
        input_hash=snapshot.input_hash,
        sources=(*base_corpus.sources, addition),
    )
    old_source_bytes = production_extraction_to_json(old_execution.extraction)["sources"][0]
    addition_plan = await service.plan_for_corpus(
        run=run,
        snapshot=snapshot,
        corpus=prospective_corpus,
        source_document_ids=frozenset({added_document_id}),
    )
    addition_execution = await service.execute(run=run, snapshot=snapshot, plan=addition_plan)
    assert addition_execution.status is ExtractionExecutionStatus.SUCCEEDED
    assert addition_execution.extraction is not None
    assert [item.source_document_id for item in addition_execution.extraction.sources] == [
        added_document_id
    ]
    assert addition_execution.model_calls == 1
    assert len(gateway.calls) == 2  # one original source call, one addition call

    merged = _merge_additive_extraction(
        old_execution.extraction,
        addition_execution.extraction,
        prospective_corpus,
    )
    merged_payload = production_extraction_to_json(merged)
    assert merged_payload["sources"][0] == old_source_bytes
    assert len(merged.sources) == 2

    retry = await service.execute(run=run, snapshot=snapshot, plan=addition_plan)
    assert retry.extraction is not None
    assert retry.model_calls == 0
    assert retry.extraction.sources[0].reuse_state is ExtractionReuseState.REUSED
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_inextractible_addition_is_refused_without_failing_the_production_run() -> None:
    subject_id, request_id = uuid4(), uuid4()
    run = ProductionRun(
        subject_id=subject_id,
        edition_id=uuid4(),
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    ready = _ready_addition(run_id=run.id, subject_id=subject_id, request_id=request_id)
    archived = replace(
        ready,
        state=SourceAdditionState.ARCHIVED,
        extraction_profile=None,
        profile_reason_code=None,
        reuse_state=None,
        extraction_profile_policy_version=None,
        checkpoint_contract_version=None,
        checkpoint_prompt_version=None,
        checkpoint_parser_version=None,
        checkpoint_verifier_version=None,
        source_text_contract_version=None,
        model_policy_version=None,
        routing_policy_version=None,
        checkpoint_id=None,
        extraction_id=None,
        model_run_id=None,
        evidence_handles=(),
    )
    uow = _JournalUow(run)
    await uow.production_source_additions.append(
        SourceAdditionJournalEntryV1(addition=archived, version=3)
    )
    context = SimpleNamespace(
        run=run,
        snapshot=SimpleNamespace(),
        references=SimpleNamespace(sources=()),
    )

    class _FailedExtraction:
        async def plan_for_corpus(self, **_kwargs: object) -> object:
            return object()

        async def execute(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                status=ExtractionExecutionStatus.BLOCKED,
                extraction=None,
                error_code="source_extraction_failed",
                model_calls=1,
            )

    service = _service(_Factory(uow))
    service._extraction_service = _FailedExtraction()
    service._load_context = lambda *_args: _async_value(context)
    service._refresh_reference_corpus = lambda *_args: _async_value(
        SimpleNamespace(sources=(object(),))
    )
    service._archived_receipt_refusal = lambda *_args: _async_value(None)

    result = await service._prepare_prospective(
        production_run_id=run.id,
        request_id=request_id,
        canonical_url=archived.canonical_url,
        entry=SourceAdditionJournalEntryV1(addition=archived, version=3),
    )

    assert result.refusal is not None
    assert result.refusal.code is SourceAdditionRefusalCode.SOURCE_EXTRACTION_FAILED
    assert result.addition.state is SourceAdditionState.REFUSED
    assert run.status is ProductionRunStatus.READY
    versions = next(iter(uow.production_source_additions.rows.values()))
    assert versions[-1].addition.diagnostic_code == "source_extraction_failed"


def test_manifest_hash_is_order_independent_and_contains_durable_lineage() -> None:
    request_id, run_id, subject_id = uuid4(), uuid4(), uuid4()
    first = _ready_addition(
        run_id=run_id, subject_id=subject_id, request_id=request_id, url="https://example.test/a"
    )
    second = _ready_addition(
        run_id=run_id, subject_id=subject_id, request_id=request_id, url="https://example.test/b"
    )

    assert source_additions_hash((first, second)) == source_additions_hash((second, first))
    manifest = source_addition_manifest((second, first))
    assert tuple(item["url"] for item in manifest) == (
        "https://example.test/a",
        "https://example.test/b",
    )
    assert manifest[0]["checkpoint_id"] == str(first.checkpoint_id)
    assert manifest[0]["model_run_id"] == str(first.model_run_id)
    assert manifest[0]["selection_criterion_version"] == "criterion-v1"
    assert source_addition_to_json(first)["state"] == "ready"


def test_legacy_source_addition_record_defaults_to_indicators_only() -> None:
    run_id, subject_id, request_id = uuid4(), uuid4(), uuid4()
    addition = _ready_addition(run_id=run_id, subject_id=subject_id, request_id=request_id)
    payload = source_addition_to_json(addition)
    payload.pop("extraction_intent")

    restored = source_addition_from_json(payload)

    assert restored.extraction_intent is SourceAdditionExtractionIntent.INDICATORS_ONLY


def test_extraction_rebase_preserves_old_bytes_and_marks_addition_context_only() -> None:
    from tests.test_production_relevance_domain import _extraction, _snapshot

    snapshot = _snapshot()
    old_id, new_id = uuid4(), uuid4()
    old_source = _source(old_id, facts=(_fact(old_id, "MOIS observed activity"),))
    new_source = _source(
        new_id,
        tier=ProductionReferenceTier.TECHNICAL,
        editorial_role=ProductionEditorialRole.CONTEXT,
        role=SourceRole.UNKNOWN,
        facts=(_fact(new_id, "Cobalt Strike was observed"),),
    )
    old_extraction = _extraction(snapshot, (old_source,))
    new_only_extraction = _extraction(snapshot, (new_source,))
    old_projection = build_relevance_projection(snapshot, old_extraction)
    corpus = SimpleNamespace(
        sources=(
            SimpleNamespace(canonical_url=old_source.canonical_url),
            SimpleNamespace(canonical_url=new_source.canonical_url),
        )
    )

    merged = _merge_additive_extraction(old_extraction, new_only_extraction, corpus)
    projection = _rebase_relevance_projection(snapshot, old_projection, merged)

    before = production_extraction_to_json(old_extraction)["sources"][0]
    after = production_extraction_to_json(merged)["sources"][0]
    assert before == after
    new_ref = next(
        item
        for item in projection.classifications
        if item.evidence_ref.source_document_id == new_id
    )
    assert new_ref.classification.value == "context"
    assert new_ref.provenance.value == "deterministic_policy"


def test_preview_exposes_critical_unarchived_counter_analysis_and_blocks_promotion() -> None:
    subject_id, request_id = uuid4(), uuid4()
    run = ProductionRun(
        subject_id=subject_id,
        edition_id=uuid4(),
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    source = ProductionReferenceSourceV1(
        canonical_url="https://example.test/krypt3ia-counter-analysis",
        tier=ProductionReferenceTier.SUPPORTING,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.UNKNOWN,
        title="Counter-analysis",
        publisher="Research team",
        published_at=None,
        source_collection_id=None,
        source_document_id=None,
        discovery_candidate_ids=(),
        collection_state=CollectionState.BLOCKED,
        content_sha256=None,
        relevance_reason="Counter-analysis for the current claim",
        proposed_by_model=False,
        eligible_for_extraction=False,
        editorial_role=ProductionEditorialRole.COUNTER_ANALYSIS,
    )
    corpus = SimpleNamespace(sources=(source,), warnings=())
    snapshot = _extraction_snapshot(subject_id)
    snapshot = replace(snapshot, production_run_id=run.id)
    addition = _ready_addition(run_id=run.id, subject_id=subject_id, request_id=request_id)
    entry: SourceAdditionJournalEntryV1
    blocked_collection = SimpleNamespace(
        id=uuid4(),
        subject_id=subject_id,
        canonical_url=source.canonical_url,
        state=CollectionState.BLOCKED,
        failure_reason_code=CollectionFailureReason.ACCESS_BLOCKED,
        source_tlp=TLP.AMBER,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    source_collections = SimpleNamespace(
        list_for_subject=lambda _subject_id: _async_value((blocked_collection,)),
        get_by_canonical_url=lambda _subject_id, _url: _async_value(blocked_collection),
    )
    source_documents = SimpleNamespace(list_for_subject=lambda _subject_id: _async_value(()))
    uow = SimpleNamespace(
        production_source_additions=SimpleNamespace(
            list_latest_for_request=lambda _run_id, _request_id: _async_value((entry,))
        ),
        source_collections=source_collections,
        source_documents=source_documents,
    )
    artifacts = tuple(
        ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject_id,
            stage=stage,
            version=1,
            input_hash="d" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=uuid4(),
        )
        for stage in (
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.RELEVANCE_PROJECTION,
        )
    )
    plan = _promotion_plan(addition, run, artifacts)
    entry = SourceAdditionJournalEntryV1(addition=addition, version=5, promotion_plan=plan)
    context = SimpleNamespace(
        run=run,
        snapshot=snapshot,
        references_artifact=artifacts[0],
        extraction_artifact=artifacts[1],
        projection_artifact=artifacts[2],
        references=corpus,
        extraction=SimpleNamespace(sources=()),
        projection=SimpleNamespace(),
        references_bytes=b"base-references",
        extraction_bytes=b"base-extraction",
        projection_bytes=b"base-projection",
    )
    service = _service(_Factory(uow))
    service._load_context = lambda _uow, _run_id: _async_value(context)
    service._effective_repair_decisions = lambda *_args: _async_value(())
    service._read_references = lambda _artifact: _async_value(corpus)
    base_hashes = {
        artifact.canonical_blob_id: hashlib.sha256(
            f"base:{artifact.id}".encode("ascii")
        ).hexdigest()
        for artifact in artifacts
    }
    service._blob_sha256 = lambda blob_id: _async_value(base_hashes[blob_id])

    async def exercise() -> None:
        preview = await service.preview(production_run_id=run.id, request_id=request_id)
        assert preview.blockers
        assert preview.blockers[0].source_url == source.canonical_url
        assert preview.blockers[0].criticality is ProductionSourceCriticality.CRITICAL
        assert not preview.ready

        service._critical_source_blockers = lambda *_args: _async_value(preview.blockers)
        with pytest.raises(SourceAdditionPromotionBlockedError):
            await service.promote_in_uow(
                _BlockedPromotionUow(run, entry, artifacts),
                production_run_id=run.id,
                request_id=request_id,
            )

    asyncio.run(exercise())


async def _async_value(value: object) -> object:
    return value


class _BlockedPromotionUow:
    """Minimal promotion transaction that proves blockers stop before writes."""

    def __init__(
        self,
        run: ProductionRun,
        entry: SourceAdditionJournalEntryV1,
        artifacts: tuple[ProductionArtifact, ...],
    ) -> None:
        self.production_runs = SimpleNamespace(
            get_for_update=lambda _run_id: _async_value(run),
            get_current_for_subject=lambda _subject_id: _async_value(run),
        )
        self.production_source_additions = SimpleNamespace(
            list_latest_for_request=lambda _run_id, _request_id: _async_value((entry,))
        )
        self.production_artifacts = SimpleNamespace(
            get_current=lambda _run_id, stage: _async_value(
                next(item for item in artifacts if item.stage.value == stage)
            ),
            get_current_for_revision=lambda _run_id, stage: _async_value(
                next(item for item in artifacts if item.stage.value == stage)
            ),
            append=self._append,
        )
        self.production_input_snapshots = SimpleNamespace(
            get_by_run=lambda _run_id: _async_value(SimpleNamespace())
        )
        self.appended: list[ProductionArtifact] = []

    async def _append(self, artifact: ProductionArtifact) -> None:
        self.appended.append(artifact)


class _BytesStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, bytes] = {}

    def put(self, content: bytes) -> UUID:
        blob_id = uuid4()
        self.rows[blob_id] = content
        return blob_id

    async def read_bytes(self, blob_id: UUID) -> bytes:
        return self.rows[blob_id]


class _TransactionFactory:
    def __init__(
        self,
        run: ProductionRun,
        entry: SourceAdditionJournalEntryV1,
        artifacts: tuple[ProductionArtifact, ...],
        snapshot: object,
    ) -> None:
        self.run = run
        self.entry = entry
        self.committed_artifacts = list(artifacts)
        self.snapshot = snapshot

    def __call__(self) -> _Context:
        return _Context(_TransactionUow(self))


class _TransactionUow:
    def __init__(self, factory: _TransactionFactory) -> None:
        self._factory = factory
        self.pending_artifacts: list[ProductionArtifact] = []
        self.production_runs = SimpleNamespace(
            get_for_update=lambda _run_id: _async_value(factory.run),
            get_current_for_subject=lambda _subject_id: _async_value(factory.run),
        )
        self.production_source_additions = SimpleNamespace(
            list_latest_for_request=lambda _run_id, _request_id: _async_value((factory.entry,))
        )
        self.production_artifacts = SimpleNamespace(
            get_current=self._get_current,
            get_current_for_revision=self._get_current,
            append=self._append,
        )
        self.production_input_snapshots = SimpleNamespace(
            get_by_run=lambda _run_id: _async_value(factory.snapshot)
        )

    async def _get_current(self, _run_id: UUID, stage: str) -> ProductionArtifact | None:
        rows = [
            artifact
            for artifact in (*self._factory.committed_artifacts, *self.pending_artifacts)
            if artifact.stage.value == stage
            and artifact.status is not ProductionArtifactStatus.STALE
        ]
        return max(rows, key=lambda item: item.version) if rows else None

    async def _append(self, artifact: ProductionArtifact) -> None:
        self.pending_artifacts.append(artifact)

    async def commit(self) -> None:
        self._factory.committed_artifacts.extend(self.pending_artifacts)
        self.pending_artifacts.clear()


def _promotion_scenario() -> tuple[
    ProductionRun,
    object,
    ProductionReferenceCorpusV1,
    SourceAdditionJournalEntryV1,
    tuple[ProductionArtifact, ...],
    _BytesStore,
]:
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    snapshot = _relevance_snapshot(subject_id=run.subject_id)
    snapshot = replace(snapshot, production_run_id=run.id)
    request_id = uuid4()
    addition = _ready_addition(
        run_id=run.id,
        subject_id=run.subject_id,
        request_id=request_id,
        url=f"https://example.test/{uuid4()}",
    )
    old_document_id = uuid4()
    old_source = _source(
        old_document_id,
        facts=(_fact(old_document_id, "MOIS observed activity"),),
    )
    new_source = _source(
        addition.source_document_id,
        tier=ProductionReferenceTier.TECHNICAL,
        editorial_role=ProductionEditorialRole.CONTEXT,
        role=SourceRole.UNKNOWN,
        facts=(_fact(addition.source_document_id, "Cobalt Strike beacon"),),
    )
    addition = replace(addition, decoded_sha256=new_source.content_sha256)
    old_reference = ProductionReferenceSourceV1(
        canonical_url=old_source.canonical_url,
        tier=ProductionReferenceTier.CORE,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        title="Core source",
        publisher="Publisher",
        published_at=None,
        source_collection_id=uuid4(),
        source_document_id=old_document_id,
        discovery_candidate_ids=(),
        collection_state=CollectionState.ARCHIVED,
        content_sha256=old_source.content_sha256,
        relevance_reason=None,
        proposed_by_model=False,
        eligible_for_extraction=True,
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    new_reference = ProductionReferenceSourceV1(
        canonical_url=addition.canonical_url,
        tier=ProductionReferenceTier.TECHNICAL,
        kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
        role=SourceRole.UNKNOWN,
        title=addition.title,
        publisher=addition.publisher,
        published_at=None,
        source_collection_id=addition.collection_id,
        source_document_id=addition.source_document_id,
        discovery_candidate_ids=(),
        collection_state=CollectionState.ARCHIVED,
        content_sha256=addition.decoded_sha256,
        relevance_reason=addition.selection_criterion,
        proposed_by_model=False,
        eligible_for_extraction=True,
        editorial_role=ProductionEditorialRole.CONTEXT,
    )
    base_corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=run.subject_id,
        research_date=snapshot.research_date,
        production_input_hash=snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(old_reference,),
        warnings=(),
    )
    projected_corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=run.subject_id,
        research_date=snapshot.research_date,
        production_input_hash=snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(old_reference, new_reference),
        warnings=(),
    )
    base_corpus_hash = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(
            production_reference_corpus_to_json(base_corpus)
        )
    ).hexdigest()
    projected_corpus_hash = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(
            production_reference_corpus_to_json(projected_corpus)
        )
    ).hexdigest()
    base_extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=run.subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=base_corpus_hash,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(old_source,),
        omitted_sources=(),
        warnings=(),
    )
    projected_addition_extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=run.subject_id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=projected_corpus_hash,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(new_source,),
        omitted_sources=(),
        warnings=(),
    )
    base_projection = build_relevance_projection(snapshot, base_extraction)
    projected_extraction = _merge_additive_extraction(
        base_extraction, projected_addition_extraction, projected_corpus
    )
    projected_projection = _rebase_relevance_projection(
        snapshot, base_projection, projected_extraction
    )
    addition_handles = tuple(
        item
        for item in extraction_evidence_refs_v1(projected_addition_extraction)
        if item.source_document_id == addition.source_document_id
    )
    projected_extraction_id = uuid4()
    addition = replace(
        addition,
        extraction_id=projected_extraction_id,
        evidence_handles=addition_handles,
    )

    store = _BytesStore()
    base_payloads = {
        ProductionArtifactStage.REFERENCES: production_reference_corpus_to_json(base_corpus),
        ProductionArtifactStage.EXTRACTION: production_extraction_to_json(base_extraction),
        ProductionArtifactStage.RELEVANCE_PROJECTION: relevance_projection_to_json(base_projection),
    }
    projected_payloads = {
        ProductionArtifactStage.REFERENCES: production_reference_corpus_to_json(projected_corpus),
        ProductionArtifactStage.EXTRACTION: production_extraction_to_json(projected_extraction),
        ProductionArtifactStage.RELEVANCE_PROJECTION: relevance_projection_to_json(
            projected_projection
        ),
    }
    base_artifacts: list[ProductionArtifact] = []
    base_fences: list[SourceAdditionArtifactFenceV1] = []
    for index, stage in enumerate(
        (
            ProductionArtifactStage.REFERENCES,
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.RELEVANCE_PROJECTION,
        )
    ):
        payload_bytes = ProductionArtifactStore.canonical_json_bytes(base_payloads[stage])
        blob_id = store.put(payload_bytes)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=1,
            input_hash=str(index + 1) * 64,
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=blob_id,
        )
        base_artifacts.append(artifact)
        base_fences.append(
            SourceAdditionArtifactFenceV1(
                stage=stage,
                artifact_id=artifact.id,
                version=artifact.version,
                input_hash=artifact.input_hash,
                canonical_sha256=hashlib.sha256(payload_bytes).hexdigest(),
            )
        )

    projected_artifacts: list[SourceAdditionArtifactProjectionV1] = []
    for stage in (
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.RELEVANCE_PROJECTION,
    ):
        payload_bytes = ProductionArtifactStore.canonical_json_bytes(projected_payloads[stage])
        blob_id = store.put(payload_bytes)
        projected_artifacts.append(
            SourceAdditionArtifactProjectionV1(
                stage=stage,
                artifact_id=(
                    projected_extraction_id
                    if stage is ProductionArtifactStage.EXTRACTION
                    else uuid4()
                ),
                version=2,
                input_hash=(
                    hashlib.sha256(payload_bytes).hexdigest()
                    if stage is ProductionArtifactStage.REFERENCES
                    else "f" * 64
                    if stage is ProductionArtifactStage.EXTRACTION
                    else projected_projection.input_hash
                ),
                canonical_blob_id=blob_id,
                canonical_sha256=hashlib.sha256(payload_bytes).hexdigest(),
            )
        )
    entry = SourceAdditionJournalEntryV1(
        addition=addition,
        version=5,
        promotion_plan=SourceAdditionPromotionPlanV1(
            production_run_id=run.id,
            subject_id=run.subject_id,
            pipeline_generation=run.pipeline_generation,
            base_artifacts=tuple(base_fences),
            projected_artifacts=tuple(projected_artifacts),
            source_additions_hash=source_additions_hash((addition,)),
        ),
    )
    return run, snapshot, base_corpus, entry, tuple(base_artifacts), store


@pytest.mark.asyncio
async def test_promotion_is_idempotent_fenced_and_rolls_back_with_downstream_failure() -> None:
    run, snapshot, corpus, entry, base_artifacts, store = _promotion_scenario()
    factory = _TransactionFactory(run, entry, base_artifacts, snapshot)
    service = _service(factory, artifact_store=store)
    service._read_references = lambda _artifact: _async_value(corpus)
    service._critical_source_blockers = lambda *_args: _async_value(())
    service._effective_repair_decisions = lambda *_args: _async_value(())

    async with factory() as uow:
        result = await service.promote_in_uow(
            uow, production_run_id=run.id, request_id=entry.addition.request_id
        )
        assert not result.already_promoted
        assert len(uow.pending_artifacts) == 3
        await uow.commit()

    old_artifacts = tuple(factory.committed_artifacts[:3])
    assert all(item.status is ProductionArtifactStatus.VERIFIED for item in old_artifacts)
    promoted_by_stage = {
        item.stage: item for item in factory.committed_artifacts if item.version == 2
    }
    assert set(promoted_by_stage) == {
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.RELEVANCE_PROJECTION,
    }
    manifest = list(source_addition_manifest((entry.addition,)))
    assert (
        promoted_by_stage[ProductionArtifactStage.REFERENCES].metadata["source_additions"]
        == manifest
    )
    assert (
        promoted_by_stage[ProductionArtifactStage.EXTRACTION].metadata["source_additions"]
        == manifest
    )

    async with factory() as replay_uow:
        replay = await service.promote_in_uow(
            replay_uow, production_run_id=run.id, request_id=entry.addition.request_id
        )
        assert replay.already_promoted
        assert not replay_uow.pending_artifacts

    run.pipeline_generation += 1
    conflict_factory = _TransactionFactory(run, entry, base_artifacts, snapshot)
    conflict_service = _service(conflict_factory, artifact_store=store)
    conflict_service._read_references = lambda _artifact: _async_value(corpus)
    conflict_service._critical_source_blockers = lambda *_args: _async_value(())
    conflict_service._effective_repair_decisions = lambda *_args: _async_value(())
    async with conflict_factory() as conflict_uow:
        with pytest.raises(SourceAdditionConflictError, match="generation_changed"):
            await conflict_service.promote_in_uow(
                conflict_uow, production_run_id=run.id, request_id=entry.addition.request_id
            )
        assert not conflict_uow.pending_artifacts

    run.pipeline_generation -= 1
    for changed_base in (
        replace(base_artifacts[0], version=base_artifacts[0].version + 1),
        replace(base_artifacts[1], input_hash="9" * 64),
    ):
        changed_artifacts = tuple(
            changed_base if item.stage is changed_base.stage else item for item in base_artifacts
        )
        fenced_factory = _TransactionFactory(run, entry, changed_artifacts, snapshot)
        fenced_service = _service(fenced_factory, artifact_store=store)
        fenced_service._read_references = lambda _artifact: _async_value(corpus)
        fenced_service._critical_source_blockers = lambda *_args: _async_value(())
        fenced_service._effective_repair_decisions = lambda *_args: _async_value(())
        async with fenced_factory() as fenced_uow:
            with pytest.raises(SourceAdditionConflictError, match="base_artifact_changed"):
                await fenced_service.promote_in_uow(
                    fenced_uow,
                    production_run_id=run.id,
                    request_id=entry.addition.request_id,
                )
            assert not fenced_uow.pending_artifacts

    rollback_factory = _TransactionFactory(run, entry, base_artifacts, snapshot)
    rollback_service = _service(rollback_factory, artifact_store=store)
    rollback_service._read_references = lambda _artifact: _async_value(corpus)
    rollback_service._critical_source_blockers = lambda *_args: _async_value(())
    rollback_service._effective_repair_decisions = lambda *_args: _async_value(())
    with pytest.raises(RuntimeError, match="synthesis_append_failed"):
        async with rollback_factory() as rollback_uow:
            await rollback_service.promote_in_uow(
                rollback_uow, production_run_id=run.id, request_id=entry.addition.request_id
            )
            raise RuntimeError("synthesis_append_failed")
    assert rollback_factory.committed_artifacts == list(base_artifacts)
